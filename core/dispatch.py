"""Concurrency layer: scope locking + dependency ordering for delegated tasks."""

import threading
import time

import routing
import memory
from paths import PROJECT_ROOT
from agent import run_agent
from providers import ToolResult

# Bounds waiting for another *executor's whole agent loop*, not one command —
# sandboxd's 30s command timeout is the wrong granularity here.
MAX_EXECUTOR_WAIT_SECONDS = 600


def _normalize(path: str) -> str:
    return str((PROJECT_ROOT / path).resolve())


def _check_no_cycles(deps: dict[str, set[str]]) -> None:
    visiting, visited = set(), set()

    def dfs(node, path):
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"circular depends_on: {' -> '.join(path + [node])}")
        visiting.add(node)
        for dep in deps.get(node, ()):
            dfs(dep, path + [node])
        visiting.discard(node)
        visited.add(node)

    for task_id in deps:
        dfs(task_id, [])


class DependencyGraph:
    """Tracks depends_on within one batch (one orchestrator turn's delegate_task calls)."""

    def __init__(self, deps: dict[str, set[str]]):
        _check_no_cycles(deps)
        self._deps = deps
        self._condition = threading.Condition()
        self._completed: dict[str, str] = {}

    def wait_for_dependencies(self, task_id: str) -> dict[str, str]:
        needed = self._deps.get(task_id, set())
        with self._condition:
            while not needed <= self._completed.keys():
                self._condition.wait()
            return {d: self._completed[d] for d in needed}

    def mark_complete(self, task_id: str, result: str) -> None:
        with self._condition:
            self._completed[task_id] = result
            self._condition.notify_all()


class ScopeScheduler:
    """Serializes tasks whose declared file scope overlaps; disjoint scope runs concurrently."""

    def __init__(self):
        self._condition = threading.Condition()
        self._active: dict[str, str] = {}  # normalized path -> task_id holding it

    def acquire(self, task_id: str, scope: list[str]) -> None:
        normalized = [_normalize(p) for p in scope]
        with self._condition:
            deadline = time.monotonic() + MAX_EXECUTOR_WAIT_SECONDS
            while any(p in self._active for p in normalized):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holders = {p: self._active[p] for p in normalized if p in self._active}
                    raise TimeoutError(f"task {task_id} waited over {MAX_EXECUTOR_WAIT_SECONDS}s for scope held by: {holders}")
                self._condition.wait(timeout=remaining)
            for p in normalized:
                self._active[p] = task_id

    def release(self, task_id: str, scope: list[str]) -> None:
        normalized = [_normalize(p) for p in scope]
        with self._condition:
            for p in normalized:
                if self._active.get(p) == task_id:
                    del self._active[p]
            self._condition.notify_all()


def run_executor_task(scheduler: ScopeScheduler, graph: DependencyGraph, task_id: str,
                       description: str, scope: list[str], external_deps: set[str],
                       preferred_model: str | None = None) -> str:
    dep_results = graph.wait_for_dependencies(task_id)
    for d in external_deps:  # earlier-turn deps: already in memory, no waiting needed
        dep_results[d] = memory.read_entry(d)
    if dep_results:
        ctx = "\n\n".join(f"[Result of {d}]:\n{r}" for d, r in dep_results.items())
        description = f"{description}\n\nContext from completed dependencies:\n{ctx}"

    acquired, model_key = False, "unknown"
    try:
        model_key = routing.pick_executor(preferred=preferred_model)  # can raise RoutingError — must not escape the thread
        scheduler.acquire(task_id, scope)    # can raise TimeoutError — same
        acquired = True
        result = run_agent(description, verbose=False, model_key=model_key, interactive=False)
    except Exception as e:
        result = f"Executor task failed: {e}"
    finally:
        if acquired:
            scheduler.release(task_id, scope)

    memory.append_entry(task_id, model_key, result, scope=scope, kind="task_result")
    graph.mark_complete(task_id, result)
    return result


def execute_delegate_tasks(calls: list, scheduler: ScopeScheduler) -> list[ToolResult]:
    """All delegate_task calls from one orchestrator turn, run as a batch of threads."""
    batch_ids = {tc.id for tc in calls}
    deps = {tc.id: set(tc.args.get("depends_on", [])) & batch_ids for tc in calls}
    external = {tc.id: set(tc.args.get("depends_on", [])) - batch_ids for tc in calls}
    graph = DependencyGraph(deps)

    results: dict[str, str] = {}

    def _run(tc):
        results[tc.id] = run_executor_task(scheduler, graph, tc.id, tc.args["description"], tc.args["scope"], external[tc.id], tc.args.get("model_key"))

    threads = [threading.Thread(target=_run, args=(tc,)) for tc in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return [ToolResult(call_id=tc.id, name="delegate_task", content=results[tc.id]) for tc in calls]
