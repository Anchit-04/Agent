"""Concurrency layer: scope locking + dependency ordering for delegated tasks."""

import threading
import time

from config import routing
import memory
import paths
from agent import run_agent, EventSink, _emit
from providers import ToolResult

# Bounds waiting for another *executor's whole agent loop*, not one command —
# sandboxd's 30s command timeout is the wrong granularity here.
MAX_EXECUTOR_WAIT_SECONDS = 600


def _normalize(path: str) -> str:
    """Scope paths are declared relative to the workspace, so two executors
    naming the same file always collide on the same lock key."""
    return str((paths.workspace() / path).resolve())


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

    def wait_for_dependencies(self, task_id: str, event_sink: "EventSink | None" = None) -> dict[str, str]:
        needed = self._deps.get(task_id, set())
        with self._condition:
            if needed and not needed <= self._completed.keys():
                _emit(event_sink, "task_status_changed", {"task_id": task_id, "status": "waiting-on-dependency"})
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

    def acquire(self, task_id: str, scope: list[str], event_sink: "EventSink | None" = None) -> None:
        normalized = [_normalize(p) for p in scope]
        with self._condition:
            deadline = time.monotonic() + MAX_EXECUTOR_WAIT_SECONDS
            if any(p in self._active for p in normalized):
                _emit(event_sink, "task_status_changed", {"task_id": task_id, "status": "waiting-on-scope"})
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
                       mem: memory.Memory, preferred_model: str | None = None,
                       event_sink: "EventSink | None" = None, injection_queue=None) -> str:
    dep_results = graph.wait_for_dependencies(task_id, event_sink=event_sink)
    for d in external_deps:  # earlier-turn deps: already in memory, no waiting needed
        dep_results[d] = mem.read_entry(d)
    if dep_results:
        ctx = "\n\n".join(f"[Result of {d}]:\n{r}" for d, r in dep_results.items())
        description = f"{description}\n\nContext from completed dependencies:\n{ctx}"

    acquired, model_key = False, "unknown"
    try:
        model_key = routing.pick_executor(preferred=preferred_model)  # can raise RoutingError — must not escape the thread
        scheduler.acquire(task_id, scope, event_sink=event_sink)    # can raise TimeoutError — same
        acquired = True
        _emit(event_sink, "task_status_changed", {"task_id": task_id, "status": "running"})
        result = run_agent(description, verbose=False, model_key=model_key, interactive=False,
                            task_id=task_id, event_sink=event_sink, mem=mem, injection_queue=injection_queue)
    except Exception as e:
        result = f"Executor task failed: {e}"
    finally:
        if acquired:
            scheduler.release(task_id, scope)

    mem.append_entry(task_id, model_key, result, scope=scope, kind="task_result")
    _emit(event_sink, "memory_entry_added", {"task_id": task_id, "agent": model_key})
    graph.mark_complete(task_id, result)
    status = "failed" if result.startswith("Executor task failed:") else "done"
    _emit(event_sink, "task_status_changed", {"task_id": task_id, "status": status})
    return result


def execute_delegate_tasks(calls: list, scheduler: ScopeScheduler, event_sink: "EventSink | None" = None,
                            mem: "memory.Memory | None" = None, injection_queue=None) -> list[ToolResult]:
    """All delegate_task calls from one orchestrator turn, run as a batch of threads.
    mem defaults to memory.default_memory() if not given (standalone use) —
    a real session always passes its own instance so every executor it
    spawns shares that session's log, not the global default."""
    mem = mem or memory.default_memory()
    batch_ids = {tc.id for tc in calls}
    deps = {tc.id: set(tc.args.get("depends_on", [])) & batch_ids for tc in calls}
    external = {tc.id: set(tc.args.get("depends_on", [])) - batch_ids for tc in calls}
    graph = DependencyGraph(deps)

    # Emitted once per task before any thread starts — this is what a client
    # rebuilds the flow graph from (structure, not just status transitions).
    for tc in calls:
        _emit(event_sink, "task_created", {
            "task_id": tc.id,
            "description": tc.args["description"],
            "scope": tc.args["scope"],
            "depends_on": tc.args.get("depends_on", []),
        })

    results: dict[str, str] = {}

    def _run(tc):
        results[tc.id] = run_executor_task(scheduler, graph, tc.id, tc.args["description"], tc.args["scope"],
                                            external[tc.id], mem, tc.args.get("model_key"),
                                            event_sink=event_sink, injection_queue=injection_queue)

    threads = [threading.Thread(target=_run, args=(tc,)) for tc in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return [ToolResult(call_id=tc.id, name="delegate_task", content=results[tc.id]) for tc in calls]
