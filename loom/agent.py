"""
mini-agent — provider-agnostic edition. Never imports a provider SDK
directly, only providers.get_provider() — swapping models is a config
change in providers/config.py, not a rewrite.

Usage:
    python agent.py "list the files in this directory and tell me what this project does"
    python agent.py "..." deepseek-chat     # optional 2nd arg: model key
"""

import sys
import json
from typing import Callable

from tools.file_tools import FILE_TOOLS, FILE_TOOL_HANDLERS
from tools.todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, TodoManager
from permissions import needs_confirmation, confirm
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input
from providers import get_provider, Turn, ToolResult
from sandbox.sandbox_client import run_bash_command
import memory

MODEL_KEY = "gemini-flash"          # default entry in providers/config.py's MODEL_REGISTRY

# WORKDIR is set in three separate places right now (not here) —
# sandbox_client.py and file_tools.py each have their own. Should centralize.

SYSTEM_PROMPT = """You are a coding agent. You have access to:
- read_file, write_file, edit_file, search_files — dedicated tools for inspecting
  and modifying code. Prefer these over bash for file operations: they're safer
  and work identically on every OS.
- todo_write — track your plan for any task with 3+ distinct steps. Write the
  full plan up front, then update it as you go: exactly one item 'in_progress'
  at a time, flipped to 'completed' the moment it's actually done.
- list_directory — list files/folders under a path. Use this instead of bash
  ls/dir/find, which behave differently across OSes.
- run_bash_command — for anything else (running tests, git, installing packages).
  You may be on Windows (cmd/PowerShell), where Unix tools like ls and find
  aren't available by default — prefer list_directory and search_files instead.

Rules:
- For non-trivial tasks, call todo_write first with your plan before doing
  anything else.
- Always read a file before editing it, so old_str matches the real content.
- For edit_file, choose old_str with enough surrounding context to be unique
  in the file — a single line is often not enough if it repeats.
- Make the smallest change that satisfies the task.
- After making changes, verify them (run tests, re-read the file, etc.) before
  declaring the task done.
- When you are finished, respond with plain text and no further tool calls.
"""

# --- Tool definitions --------------------------------------------------------
# Plain JSON-schema dicts — each provider adapter converts these to its own
# tool format (see providers/*.py).

BASH_TOOL = {
    "type": "function",
    "name": "run_bash_command",
    "description": (
        "Execute a bash command in the project working directory and "
        "return its stdout, stderr, and exit code. Use this to read "
        "files, write files, search code, and run tests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            }
        },
        "required": ["command"],
    },
}

ALL_TOOLS = [BASH_TOOL] + FILE_TOOLS + TODO_TOOLS + memory.MEMORY_TOOLS

# event_sink is None by default (no server attached) — purely additive, the
# single-agent CLI is unaffected.
EventSink = Callable[[str, dict], None]


def _emit(event_sink: "EventSink | None", event_type: str, payload: dict) -> None:
    if event_sink is not None:
        event_sink(event_type, payload)


# --- Tool execution -----------------------------------------------------------

def execute_tool(name: str, args: dict, model_key: str, todo_manager: TodoManager, mem: memory.Memory) -> str:
    if name == "run_bash_command":
        return run_bash_command(args["command"])
    if name in FILE_TOOL_HANDLERS:
        return FILE_TOOL_HANDLERS[name](args)
    if name in TODO_TOOL_HANDLERS:
        return TODO_TOOL_HANDLERS[name](todo_manager, args)
    if name in memory.MEMORY_TOOL_HANDLERS:
        return memory.MEMORY_TOOL_HANDLERS[name](mem, model_key, args)
    return json.dumps({"error": f"Unknown tool: {name}"})


# --- The agent loop -----------------------------------------------------------

def run_agent(task: str, verbose: bool = True, model_key: str = MODEL_KEY, interactive: bool = True,
              task_id: str | None = None, event_sink: "EventSink | None" = None,
              mem: "memory.Memory | None" = None, injection_queue=None) -> str:
    """interactive=False skips the confirm() gate — used by concurrent
    executors, since input() breaks across threads. task_id/event_sink/mem/
    injection_queue are the server hooks (all optional, all defaulted so the
    plain CLI still works unchanged)."""
    provider = get_provider(model_key)
    todo_manager = TodoManager()  # own instance per run — never shared, no lock needed
    mem = mem or memory.DEFAULT_MEMORY

    # This list IS the conversation — resent in full every call.
    history = [Turn(role="user", text=task)]

    turn_count = 0
    final_text = ""
    while True:
        if injection_queue is not None:
            for msg in injection_queue.drain(task_id):
                history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
                _emit(event_sink, "human_message_injected", {"task_id": task_id, "content": msg})
        response = provider.generate(history, ALL_TOOLS, SYSTEM_PROMPT)

        if response.text:
            final_text = response.text
            if verbose:
                print(f"\n\033[94m{model_key}:\033[0m {response.text}")
            _emit(event_sink, "agent_turn", {"task_id": task_id, "model_key": model_key, "text": response.text})

        if not response.tool_calls:
            break  # model is done: no more tools requested

        turn_count += 1
        if turn_count >= MAX_ITERATIONS:
            if verbose:
                print(
                    f"\n\033[91mStopping: hit the {MAX_ITERATIONS}-turn safety cap. "
                    "The task may be stuck in a loop, or may just be larger than "
                    "this harness is tuned for. Check the plan above for progress "
                    "made so far.\033[0m"
                )
            _emit(event_sink, "error", {"task_id": task_id, "message": f"hit the {MAX_ITERATIONS}-turn safety cap"})
            break

        # Assistant turn has to land in history before its tool results do.
        history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls))

        tool_results = []
        executed_results = []  # kept alongside tool_results for compaction summaries
        for tc in response.tool_calls:
            args = tc.args

            if verbose and tc.name == "run_bash_command":
                print(f"\033[93m$ {args.get('command', '')}\033[0m")
            _emit(event_sink, "tool_call", {"task_id": task_id, "name": tc.name, "args": args})

            if tc.raw and tc.raw.get("args_parse_error"):
                # Adapter couldn't parse this call's JSON args — don't execute
                # with empty/wrong args, hand back a tool-error the model can react to.
                raw_args = tc.raw.get("raw_arguments", "")
                result = json.dumps({
                    "error": f"Could not parse arguments for {tc.name}: {tc.raw['args_parse_error']}. "
                             f"Raw arguments received: {raw_args[:300]!r}. "
                             "Retry the call with valid JSON arguments."
                })
            elif interactive and needs_confirmation(tc.name, args):
                if confirm(tc.name, args):
                    result = execute_tool(tc.name, args, model_key, todo_manager, mem)
                else:
                    print("\033[91mDenied.\033[0m")
                    result = json.dumps({
                        "error": "User denied this action. Do not retry it as-is — "
                                 "explain what you were trying to do and ask how to proceed, "
                                 "or try a different approach."
                    })
            else:
                result = execute_tool(tc.name, args, model_key, todo_manager, mem)

            if verbose:
                if tc.name == "todo_write":
                    checklist = todo_manager.render()
                    print(f"\033[96mPlan:\033[0m\n{checklist}")
                else:
                    print(result[:500])
            _emit(event_sink, "tool_result", {"task_id": task_id, "name": tc.name, "content": result})
            if tc.name == "todo_write":
                _emit(event_sink, "todo_updated", {"task_id": task_id, "checklist": todo_manager.render()})

            tool_results.append(ToolResult(call_id=tc.id, name=tc.name, content=result))
            executed_results.append({"name": tc.name, "content": result})

        if turn_count % COMPACT_EVERY == 0:
            # Drop the raw history, replace it with a summary — the todo list
            # already externalizes progress, so nothing essential is lost.
            if verbose:
                print(f"\033[95m[context] Compacting after {turn_count} turns — replacing history with a summary.\033[0m")
            compact_text = build_compact_input(task, todo_manager.render(), executed_results)
            history = [Turn(role="user", text=compact_text)]
        else:
            history.append(Turn(role="user", tool_results=tool_results))

    return final_text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent.py '<task description>' [model_key]")
        sys.exit(1)
    key = sys.argv[2] if len(sys.argv) > 2 else MODEL_KEY
    run_agent(sys.argv[1], model_key=key)
