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

from tools import file_tools
from tools.file_tools import FILE_TOOLS, FILE_TOOL_HANDLERS
from tools.todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, TodoManager
import paths
import permissions
from approval import CLIApprover, DenyApprover
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input
from providers import get_provider, Turn, ToolResult
from sandbox.sandbox_client import run_bash_command
import memory

MODEL_KEY = "gemini-flash"          # default entry in providers/config.py's MODEL_REGISTRY

# WORKDIR is set in three separate places right now (not here) —
# sandbox_client.py and file_tools.py each have their own. Should centralize.

# Prepended to every system prompt, orchestrator included. The model key is
# interpolated rather than hardcoded so the agent can answer "what are you
# running on?" accurately, while still presenting as Fox by default — the
# underlying vendor name should never appear in an introduction.
IDENTITY = """You are Fox, a coding agent.

- Introduce yourself as Fox. Never identify as the underlying model, and
  never as an assistant built by whoever trained that model.
- You are currently running on the model '{model_key}'. Say so only if the
  user actually asks which model, engine, or provider you're using. Don't
  volunteer it, and never put it in an introduction.
- Introduce yourself once, when greeted or asked who you are — then get on
  with the work. Do not re-introduce yourself at the start of every reply.

"""

SYSTEM_PROMPT_BASE = """You have access to:
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


def build_system_prompt(model_key: str) -> str:
    """Fox identity + the tool/behaviour rules. Built per run rather than
    stored as a constant, since the identity block names the live model."""
    return IDENTITY.format(model_key=model_key) + SYSTEM_PROMPT_BASE

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
              mem: "memory.Memory | None" = None, injection_queue=None, keep_alive: bool = False,
              mode: str = permissions.WORKSPACE_WRITE, approver=None) -> str:
    """mode is the sandbox axis (read-only / workspace-write / full-access);
    `approver` is the approval axis — who gets asked when policy says ASK.
    They're independent on purpose: see permissions.py.

    interactive only picks the default approver now (CLI prompt vs refuse).
    It no longer switches the permission check off — passing interactive=False
    used to bypass permissions entirely, which is how a server session could
    write files with nobody consulted. Callers with a real route to a human
    (the server) pass an ApprovalGate explicitly.

    task_id/event_sink/mem/injection_queue are the server hooks (all optional,
    all defaulted so the plain CLI still works unchanged). keep_alive=True is
    for a live top-level chat session only — never set it for a delegated
    executor task, or it'll block forever waiting for input nobody's going to
    send, deadlocking whatever's waiting on this task via
    DependencyGraph.wait_for_dependencies()."""
    provider = get_provider(model_key)
    # Start from no grants, whatever happened on this thread before. Clearing
    # only on the way out would miss a run that raised, and two sequential
    # run_agent() calls on one thread would share approvals.
    file_tools.clear_approved_paths()
    approver = approver or (CLIApprover() if interactive else DenyApprover())
    system_prompt = build_system_prompt(model_key)  # model_key is fixed for this run
    todo_manager = TodoManager()  # own instance per run — never shared, no lock needed
    mem = mem or memory.default_memory()

    # This list IS the conversation — resent in full every call.
    history = [Turn(role="user", text=task)]

    turn_count = 0
    final_text = ""
    while True:
        if injection_queue is not None:
            for msg in injection_queue.drain(task_id):
                history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
                _emit(event_sink, "human_message_injected", {"task_id": task_id, "content": msg})
        response = provider.generate(history, ALL_TOOLS, system_prompt)

        if response.text:
            final_text = response.text
            if verbose:
                print(f"\n\033[94m{model_key}:\033[0m {response.text}")
            _emit(event_sink, "agent_turn", {"task_id": task_id, "model_key": model_key, "text": response.text})

        if not response.tool_calls:
            if keep_alive and injection_queue is not None:
                # Model has nothing more to do right now — that means "your
                # turn," not "session over." Block here for the next message
                # instead of ending; only close() (an explicit end-of-session
                # signal) or a real disconnect-driven close should end this.
                messages = injection_queue.wait_for_message(task_id)
                if messages is None:
                    break
                for msg in messages:
                    history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
                    _emit(event_sink, "human_message_injected", {"task_id": task_id, "content": msg})
                continue
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
            else:
                decision = permissions.classify(tc.name, args, mode)
                if decision.action == permissions.ALLOW:
                    result = execute_tool(tc.name, args, model_key, todo_manager, mem)
                elif decision.action == permissions.DENY:
                    # Refused by the session's mode, not by a person — say so,
                    # so the model stops rather than re-asking a locked door.
                    _emit(event_sink, "tool_denied", {"task_id": task_id, "name": tc.name,
                                                      "reason": decision.reason})
                    result = json.dumps({
                        "error": f"Refused: {decision.reason}. This session's permission "
                                 "mode forbids it — don't retry, and don't work around it. "
                                 "Tell the user what you needed and why."
                    })
                elif approver.approve(tc.name, args, decision.reason):
                    # Consent has to actually grant the thing. The file tools
                    # are workspace-bound in _resolve(), so without this an
                    # approved out-of-workspace write would still fail — the
                    # prompt would be asking a question it couldn't honour.
                    # Covers "always" too: approve() returns True without
                    # re-asking, and each new path still gets granted here.
                    if tc.name in permissions.PATH_TOOLS and args.get("path"):
                        file_tools.allow_path(args["path"])
                    result = execute_tool(tc.name, args, model_key, todo_manager, mem)
                else:
                    if verbose:
                        print("\033[91mDenied.\033[0m")
                    _emit(event_sink, "tool_denied", {"task_id": task_id, "name": tc.name,
                                                      "reason": "denied by user"})
                    result = json.dumps({
                        "error": "User denied this action. Do not retry it as-is — "
                                 "explain what you were trying to do and ask how to proceed, "
                                 "or try a different approach."
                    })

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

    # Grants are per-run, not per-thread-lifetime: executor threads are created
    # per batch but nothing guarantees they aren't reused, and a later run must
    # never inherit approvals a human gave to an earlier one.
    file_tools.clear_approved_paths()
    return final_text


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run one Fox agent from the terminal.")
    parser.add_argument("task", help="What the agent should do.")
    parser.add_argument("model_key", nargs="?", default=MODEL_KEY,
                        help=f"Model registry key (default: {MODEL_KEY}).")
    parser.add_argument("--workspace", default=None,
                        help="Directory the agent may read and write. Defaults to the "
                             "current directory.")
    parser.add_argument("--allow-unsafe-workspace", action="store_true",
                        help="Permit a drive root or your home directory as the workspace.")
    parser.add_argument("--mode", default=permissions.WORKSPACE_WRITE, choices=permissions.MODES,
                        help="Permission mode (default: workspace-write).")
    args = parser.parse_args()

    try:
        ws_root = paths.set_workspace(args.workspace or paths.workspace(),
                                      allow_unsafe=args.allow_unsafe_workspace)
    except paths.WorkspaceError as e:
        raise SystemExit(f"error: {e}")

    print(f"\033[90mworkspace: {ws_root}\033[0m")
    run_agent(args.task, model_key=args.model_key, mode=args.mode)
