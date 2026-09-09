"""Planning orchestrator: delegates work to executors, never touches files/bash itself."""

import json
import sys

from config import routing, preferences
import memory
import dispatch
from injection import InjectionQueue
from agent import EventSink, _emit, IDENTITY
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input
from providers import get_provider, Turn, ToolResult
from tools.todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, TodoManager

DELEGATE_TASK_TOOL = {
    "type": "function",
    "name": "delegate_task",
    "description": (
        "Delegate a sub-task to an executor agent with the full tool set (bash, file tools, todo). "
        "You never touch files or run commands yourself. Call multiple times in one turn for "
        "independent sub-tasks — they run concurrently; overlapping scope is serialized automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Self-contained instructions — no visibility into your reasoning."},
            "scope": {"type": "array", "items": {"type": "string"}, "description": "File paths this task touches. Empty if none."},
            "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Task ids that must finish first; their results are auto-included in this task's context."},
            "model_key": {"type": "string", "description": "Optional — request a specific executor (e.g. 'kimi-k2' for frontend/design, 'deepseek-chat' for backend). See executor specialties below. Omit to let the system pick any available cheap-tier model."},
        },
        "required": ["description", "scope"],
    },
}

ORCHESTRATOR_SYSTEM_PROMPT_BASE = """You are the planning orchestrator for a coding task.
You never touch files or run commands yourself — only delegate_task, todo_write, memory_write, memory_read.

Rules:
- Break the task into well-scoped sub-tasks, delegate each with delegate_task.
- Declare `scope` honestly and completely — it's what lets independent tasks run concurrently.
- Use `depends_on` when a task needs another task's actual output, even if scope doesn't overlap.
- If a sub-task matches a listed executor specialty, or the task instructions say to use a
  specific model for certain work, pass that model via `model_key`. Otherwise omit it.
- Call delegate_task multiple times in one turn for independent sub-tasks.
- Use todo_write to track your plan.
- The current shared memory index is below — pull a specific entry with memory_read if relevant.
- When done, respond with plain text and no further tool calls.
"""

ALL_ORCHESTRATOR_TOOLS = [DELEGATE_TASK_TOOL] + TODO_TOOLS + memory.MEMORY_TOOLS


def execute_orchestrator_tool(name: str, args: dict, model_key: str, todo_manager: TodoManager, mem: memory.Memory) -> str:
    if name in TODO_TOOL_HANDLERS:
        return TODO_TOOL_HANDLERS[name](todo_manager, args)
    if name in memory.MEMORY_TOOL_HANDLERS:
        return memory.MEMORY_TOOL_HANDLERS[name](mem, model_key, args)
    return json.dumps({"error": f"Unknown orchestrator tool: {name}"})


def run_orchestrator(task: str, verbose: bool = True, model_key: str | None = None,
                      event_sink: "EventSink | None" = None, mem: "memory.Memory | None" = None,
                      injection_queue: "InjectionQueue | None" = None) -> str:
    """task_id=None on emitted events means the orchestrator itself, same
    convention dispatch.py uses for delegated tasks. mem/injection_queue
    default to fresh/standalone instances but are meant to be passed in by
    the server, shared with every executor this run spawns."""
    model_key = model_key or routing.pick_orchestrator()
    provider = get_provider(model_key)
    todo_manager = TodoManager()  # own instance per session — never shared, no lock needed
    mem = mem or memory.DEFAULT_MEMORY
    injection_queue = injection_queue or InjectionQueue()
    scheduler = dispatch.ScopeScheduler()

    history = [Turn(role="user", text=task)]
    turn_count, final_text = 0, ""

    while True:
        for msg in injection_queue.drain(None):  # None = messages addressed to the orchestrator itself
            history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
            _emit(event_sink, "human_message_injected", {"task_id": None, "content": msg})
        system_prompt = (
            IDENTITY.format(model_key=model_key)
            + ORCHESTRATOR_SYSTEM_PROMPT_BASE
            + "\n## Executor specialties\n" + preferences.render_for_prompt()
            + "\n## Current shared memory index\n" + mem.read_index()
        )
        response = provider.generate(history, ALL_ORCHESTRATOR_TOOLS, system_prompt)

        if response.text:
            final_text = response.text
            if verbose:
                print(f"\n\033[94m[orchestrator/{model_key}]:\033[0m {response.text}")
            _emit(event_sink, "agent_turn", {"task_id": None, "model_key": model_key, "text": response.text})

        if not response.tool_calls:
            break
        turn_count += 1
        if turn_count >= MAX_ITERATIONS:
            if verbose:
                print(f"\n\033[91mStopping: hit the {MAX_ITERATIONS}-turn safety cap.\033[0m")
            _emit(event_sink, "error", {"task_id": None, "message": f"hit the {MAX_ITERATIONS}-turn safety cap"})
            break

        history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls))

        delegate_calls = [tc for tc in response.tool_calls if tc.name == "delegate_task"]
        other_calls = [tc for tc in response.tool_calls if tc.name != "delegate_task"]

        results_by_id: dict[str, ToolResult] = {}
        if delegate_calls:
            for r in dispatch.execute_delegate_tasks(delegate_calls, scheduler, event_sink=event_sink, mem=mem, injection_queue=injection_queue):
                results_by_id[r.call_id] = r
        for tc in other_calls:
            _emit(event_sink, "tool_call", {"task_id": None, "name": tc.name, "args": tc.args})
            content = execute_orchestrator_tool(tc.name, tc.args, model_key, todo_manager, mem)
            _emit(event_sink, "tool_result", {"task_id": None, "name": tc.name, "content": content})
            if tc.name == "todo_write":
                _emit(event_sink, "todo_updated", {"task_id": None, "checklist": todo_manager.render()})
            results_by_id[tc.id] = ToolResult(call_id=tc.id, name=tc.name, content=content)

        tool_results = [results_by_id[tc.id] for tc in response.tool_calls]  # preserve original order

        if verbose:
            for r in tool_results:
                print(f"\033[93m[{r.name}]\033[0m {r.content[:300]}")

        if turn_count % COMPACT_EVERY == 0:
            compact_text = build_compact_input(task, todo_manager.render(), [{"name": r.name, "content": r.content} for r in tool_results])
            history = [Turn(role="user", text=compact_text)]
        else:
            history.append(Turn(role="user", tool_results=tool_results))

    return final_text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py '<task description>'")
        sys.exit(1)
    run_orchestrator(sys.argv[1])
