"""Planning orchestrator: delegates work to executors, never touches files/bash itself."""

import json
import sys

import routing
import memory
import dispatch
import preferences
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input
from providers import get_provider, Turn, ToolResult
from todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, render_todos, reset_todos

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


def execute_orchestrator_tool(name: str, args: dict, model_key: str) -> str:
    if name in TODO_TOOL_HANDLERS:
        return TODO_TOOL_HANDLERS[name](args)
    if name in memory.MEMORY_TOOL_HANDLERS:
        return memory.MEMORY_TOOL_HANDLERS[name](model_key, args)
    return json.dumps({"error": f"Unknown orchestrator tool: {name}"})


def run_orchestrator(task: str, verbose: bool = True, model_key: str | None = None) -> str:
    model_key = model_key or routing.pick_orchestrator()
    provider = get_provider(model_key)
    reset_todos()
    scheduler = dispatch.ScopeScheduler()

    history = [Turn(role="user", text=task)]
    turn_count, final_text = 0, ""

    while True:
        system_prompt = (
            ORCHESTRATOR_SYSTEM_PROMPT_BASE
            + "\n## Executor specialties\n" + preferences.render_for_prompt()
            + "\n## Current shared memory index\n" + memory.read_index()
        )
        response = provider.generate(history, ALL_ORCHESTRATOR_TOOLS, system_prompt)

        if response.text:
            final_text = response.text
            if verbose:
                print(f"\n\033[94m[orchestrator/{model_key}]:\033[0m {response.text}")

        if not response.tool_calls:
            break
        turn_count += 1
        if turn_count >= MAX_ITERATIONS:
            if verbose:
                print(f"\n\033[91mStopping: hit the {MAX_ITERATIONS}-turn safety cap.\033[0m")
            break

        history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls))

        delegate_calls = [tc for tc in response.tool_calls if tc.name == "delegate_task"]
        other_calls = [tc for tc in response.tool_calls if tc.name != "delegate_task"]

        results_by_id: dict[str, ToolResult] = {}
        if delegate_calls:
            for r in dispatch.execute_delegate_tasks(delegate_calls, scheduler):
                results_by_id[r.call_id] = r
        for tc in other_calls:
            content = execute_orchestrator_tool(tc.name, tc.args, model_key)
            results_by_id[tc.id] = ToolResult(call_id=tc.id, name=tc.name, content=content)

        tool_results = [results_by_id[tc.id] for tc in response.tool_calls]  # preserve original order

        if verbose:
            for r in tool_results:
                print(f"\033[93m[{r.name}]\033[0m {r.content[:300]}")

        if turn_count % COMPACT_EVERY == 0:
            compact_text = build_compact_input(task, render_todos(), [{"name": r.name, "content": r.content} for r in tool_results])
            history = [Turn(role="user", text=compact_text)]
        else:
            history.append(Turn(role="user", tool_results=tool_results))

    return final_text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py '<task description>'")
        sys.exit(1)
    run_orchestrator(sys.argv[1])
