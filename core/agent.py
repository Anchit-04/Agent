"""
mini-agent, Gemini edition — self-managed memory.

Same loop as agent.py (the Anthropic version): the harness owns the full
conversation history as a local list and resends it in full on every call.
Uses client.models.generate_content (Gemini's stateless endpoint) instead of
the Interactions API's previous_interaction_id chaining — so this loop is
now structurally identical to the Anthropic version, just with Gemini's
Content/Part shapes instead of Anthropic's messages/content-block shapes.

This is a deliberate architecture choice, not just a style preference: owning
the conversation locally is what makes it possible to (a) actually compact
context yourself instead of restarting a server-side chain, and (b) swap in
a different provider later without redesigning the loop — only the small
translation layer between "our contents list" and "what this provider's API
wants" would need to change.

Usage:
    export GEMINI_API_KEY=AIza...
    python agent_gemini.py "list the files in this directory and tell me what this project does"

Get a free key at https://aistudio.google.com/apikey (free tier, rate-limited,
no card required).
"""

import re
import sys
import json
import time
import subprocess

from google import genai
from google.genai import types

from file_tools import FILE_TOOLS, FILE_TOOL_HANDLERS
from todo_tool import TODO_TOOLS, TODO_TOOL_HANDLERS, render_todos, reset_todos
from permissions import needs_confirmation, confirm
from context import MAX_ITERATIONS, COMPACT_EVERY, build_compact_input

MODEL = "gemini-3.6-flash"          # fast + free-tier friendly
WORKDIR = "."                        # change this to sandbox the agent to a project folder
MAX_RETRIES = 5                      # how many times to retry a rate-limited call

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

# --- Tool definition -----------------------------------------------------
# Our own schemas stay plain dicts (JSON-schema shaped) so they're reusable
# across providers unchanged — only the conversion into each provider's SDK
# types (build_gemini_tool, below) is provider-specific.

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

ALL_TOOLS = [BASH_TOOL] + FILE_TOOLS + TODO_TOOLS


def build_gemini_tool(tool_schemas: list) -> types.Tool:
    """Convert our provider-agnostic tool schemas into a Gemini Tool object."""
    declarations = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=t["parameters"],
        )
        for t in tool_schemas
    ]
    return types.Tool(function_declarations=declarations)


GEMINI_TOOL = build_gemini_tool(ALL_TOOLS)
GEMINI_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[GEMINI_TOOL],
    # We orchestrate the tool-call loop ourselves — turn off the SDK's
    # automatic function calling so it doesn't try to execute anything itself.
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)


def generate_with_retry(client: genai.Client, contents: list):
    """
    Wrapper around client.models.generate_content() that retries on 429s.

    The free tier is rate-limited (as low as 5 requests/minute on some
    models), and an agent loop burns one request per tool round-trip, so
    getting throttled mid-task is expected, not a bug. We back off and
    retry instead of crashing.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return client.models.generate_content(
                model=MODEL, contents=contents, config=GEMINI_CONFIG
            )
        except Exception as e:
            msg = str(e)
            if "429" not in msg and "quota" not in msg.lower():
                raise  # not a rate-limit error, don't swallow it

            match = re.search(r"retry in ([\d.]+)s", msg)
            wait = float(match.group(1)) + 1 if match else (2 ** attempt)

            if attempt == MAX_RETRIES - 1:
                raise
            print(f"\033[91mRate limited, waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
            time.sleep(wait)


# --- Tool execution --------------------------------------------------------

def run_bash_command(command: str, timeout: int = 30) -> str:
    """Execute a shell command and return a bounded, agent-readable result."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        MAX_CHARS = 8000
        if len(output) > MAX_CHARS:
            output = output[:MAX_CHARS] + f"\n... [truncated, {len(output)} chars total]"
        return json.dumps({"exit_code": result.returncode, "output": output})
    except subprocess.TimeoutExpired:
        return json.dumps({"exit_code": -1, "output": f"Command timed out after {timeout}s"})


def execute_tool(name: str, args: dict) -> str:
    if name == "run_bash_command":
        return run_bash_command(args["command"])
    if name in FILE_TOOL_HANDLERS:
        return FILE_TOOL_HANDLERS[name](args)
    if name in TODO_TOOL_HANDLERS:
        return TODO_TOOL_HANDLERS[name](args)
    return json.dumps({"error": f"Unknown tool: {name}"})


# --- The agent loop ---------------------------------------------------------

def run_agent(task: str, verbose: bool = True) -> None:
    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    reset_todos()  # fresh plan state per task, not carried over from a prior run

    # This list IS the conversation. We own it, we grow it, we can compact
    # it — nothing about it depends on Gemini's server retaining anything.
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]

    turn_count = 0
    while True:
        response = generate_with_retry(client, contents)

        if response.text and verbose:
            print(f"\n\033[94mGemini:\033[0m {response.text}")

        function_calls = response.function_calls
        if not function_calls:
            break  # model is done: no more tools requested

        turn_count += 1
        if turn_count >= MAX_ITERATIONS:
            print(
                f"\n\033[91mStopping: hit the {MAX_ITERATIONS}-turn safety cap. "
                "The task may be stuck in a loop, or may just be larger than "
                "this harness is tuned for. Check the plan above for progress "
                "made so far.\033[0m"
            )
            break

        # The model's turn (including its function_call parts) must be
        # appended before we append our function_response — Gemini expects
        # to see its own prior turn in the history, same as Anthropic does.
        contents.append(response.candidates[0].content)

        # Execute every function call this turn and collect results.
        response_parts = []
        executed_results = []  # kept alongside response_parts for compaction summaries
        for fc in function_calls:
            args = dict(fc.args) if fc.args else {}

            if verbose and fc.name == "run_bash_command":
                print(f"\033[93m$ {args.get('command', '')}\033[0m")

            if needs_confirmation(fc.name, args):
                if confirm(fc.name, args):
                    result = execute_tool(fc.name, args)
                else:
                    print("\033[91mDenied.\033[0m")
                    result = json.dumps({
                        "error": "User denied this action. Do not retry it as-is — "
                                 "explain what you were trying to do and ask how to proceed, "
                                 "or try a different approach."
                    })
            else:
                result = execute_tool(fc.name, args)

            if verbose:
                if fc.name == "todo_write":
                    checklist = render_todos()
                    print(f"\033[96mPlan:\033[0m\n{checklist}")
                else:
                    print(result[:500])

            response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"result": result})
            )
            executed_results.append({"name": fc.name, "content": result})

        if turn_count % COMPACT_EVERY == 0:
            # Real compaction now — we just replace our own local list, no
            # server-side chain to abandon. See context.py for why nothing
            # essential is lost (the todo list already externalizes progress).
            if verbose:
                print(f"\033[95m[context] Compacting after {turn_count} turns — replacing history with a summary.\033[0m")
            compact_text = build_compact_input(task, render_todos(), executed_results)
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=compact_text)])]
        else:
            # Gemini's accepted roles are USER and MODEL only — no separate
            # 'tool' role (unlike Anthropic, where tool_result messages are
            # role='user' too, so this is actually the same convention).
            contents.append(types.Content(role="user", parts=response_parts))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent_gemini.py '<task description>'")
        sys.exit(1)
    run_agent(sys.argv[1])