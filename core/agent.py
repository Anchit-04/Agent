"""
mini-agent — provider-agnostic edition.

The harness owns the full conversation history locally as a list of neutral
Turn objects (providers/base.py) and resends it in full on every call — same
loop shape no matter which model is answering. Which model that is comes
entirely from providers/config.py's MODEL_REGISTRY; this file never imports
a provider SDK directly, only providers.get_provider(). Swapping models, or
later routing different steps of the same task to different models, is a
config change here, not a rewrite.

Usage:
    python agent.py "list the files in this directory and tell me what this project does"
    python agent.py "..." deepseek-chat     # optional 2nd arg: model key from providers/config.py

Each entry in MODEL_REGISTRY documents which env var needs to hold that
provider's API key (set it in .env).
"""

import sys
import json
from typing import Callable

from file_tools import FILE_TOOLS, FILE_TOOL_HANDLERS
from todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, TodoManager
from permissions import needs_confirmation, confirm
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input
from providers import get_provider, Turn, ToolResult
from sandbox_client import run_bash_command
import memory

MODEL_KEY = "gemini-flash"          # default entry in providers/config.py's MODEL_REGISTRY

# NOTE: the working directory the agent is sandboxed to is currently set in
# three separate places — here's not one of them anymore (bash execution
# moved to sandbox_client.py's own WORKDIR); file_tools.py has its own too.
# Real fix is centralizing this into one shared config module — tracked as
# a follow-up, not done in this change to keep it scoped to "wire up
# sandboxd," but worth knowing before you go looking for "the" WORKDIR.

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

# --- Tool definitions -----------------------------------------------------
# Plain dicts (JSON-schema shaped) so they're reusable across providers
# unchanged — each provider adapter converts them into its own SDK's tool
# format internally (see providers/*.py).

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

# One callback shape used everywhere events get emitted: (event_type, payload).
# None (the default) means "no server attached" — behaves exactly as before
# this refactor, purely additive so the single-agent CLI is unaffected.
EventSink = Callable[[str, dict], None]


def _emit(event_sink: "EventSink | None", event_type: str, payload: dict) -> None:
    if event_sink is not None:
        event_sink(event_type, payload)


# --- Tool execution ---------------------------------------------------------

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


# --- The agent loop ---------------------------------------------------------

def run_agent(task: str, verbose: bool = True, model_key: str = MODEL_KEY, interactive: bool = True,
              task_id: str | None = None, event_sink: "EventSink | None" = None,
              mem: "memory.Memory | None" = None, injection_queue=None) -> str:
    """interactive=False skips the confirm() gate — used when running as a
    concurrent executor (Phase 5), since input() across threads is broken.
    task_id/event_sink are Phase 6 additions for the server: task_id tags
    every emitted event (None for standalone CLI use, a real id when this
    is one of dispatch.py's executors); event_sink is None by default so
    nothing changes for the existing single-agent CLI. mem defaults to
    memory.DEFAULT_MEMORY (the single-agent CLI's shared file) — dispatch.py
    passes a session-scoped instance instead when running as an executor.
    injection_queue (injection.InjectionQueue) lets a human's mid-flight
    message reach this specific task_id — drained once per loop iteration,
    right before the next provider.generate() call, never mid-tool-call."""
    provider = get_provider(model_key)
    todo_manager = TodoManager()  # own instance per run — never shared, no lock needed
    mem = mem or memory.DEFAULT_MEMORY

    # This list IS the conversation. We own it, we grow it, we can compact
    # it — nothing about it depends on any provider retaining state.
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

        # The model's turn (including its tool calls) must be appended
        # before we append the tool results — every provider expects to see
        # its own prior turn in history before the results it's waiting on.
        history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls))

        # Execute every tool call this turn and collect results.
        tool_results = []
        executed_results = []  # kept alongside tool_results for compaction summaries
        for tc in response.tool_calls:
            args = tc.args

            if verbose and tc.name == "run_bash_command":
                print(f"\033[93m$ {args.get('command', '')}\033[0m")
            _emit(event_sink, "tool_call", {"task_id": task_id, "name": tc.name, "args": args})

            if tc.raw and tc.raw.get("args_parse_error"):
                # Provider adapter couldn't parse this call's arguments as
                # JSON (see openai_compatible.py) — don't attempt to execute
                # it with empty/wrong args, just hand the model back a
                # normal tool-error result it can react to.
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
            # Real compaction — just replace our own local list, no
            # server-side chain to abandon. See context.py for why nothing
            # essential is lost (the todo list already externalizes progress).
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
