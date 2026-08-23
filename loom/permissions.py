 
# Tools that always execute without asking.
SAFE_TOOLS = {"read_file", "search_files", "todo_write", "list_directory", "memory_write", "memory_read"}
 
# Tools that require confirmation unless explicitly allowed below.
CONFIRM_TOOLS = {"write_file", "edit_file", "run_bash_command"}
 
# Read-only bash commands that are safe enough to auto-approve even though
# run_bash_command is otherwise gated. Prefix match, case-insensitive.
SAFE_BASH_PREFIXES = (
    "ls", "dir", "cat", "type", "pwd", "echo", "find ", "grep", "git status",
    "git diff", "git log", "git show", "python --version", "python3 --version",
    "pip list", "pip show",
)
 
# Tool names the user has approved for the rest of this session via "always".
_session_approved: set = set()
 
 
def _is_safe_bash(command: str) -> bool:
    cmd = command.strip().lower()
    return any(cmd.startswith(prefix.lower()) for prefix in SAFE_BASH_PREFIXES)
 
 
def needs_confirmation(tool_name: str, args: dict) -> bool:
    if tool_name in SAFE_TOOLS:
        return False
    if tool_name not in CONFIRM_TOOLS:
        return True  # unknown tool: err toward confirming
    if tool_name in _session_approved:
        return False
    if tool_name == "run_bash_command" and _is_safe_bash(args.get("command", "")):
        return False
    return True
 
 
def _describe(tool_name: str, args: dict) -> str:
    if tool_name == "run_bash_command":
        return f"run bash command: {args.get('command', '')}"
    if tool_name == "write_file":
        preview = args.get("content", "")[:200]
        return f"write to {args.get('path')} ({len(args.get('content', ''))} chars):\n---\n{preview}{'...' if len(args.get('content', '')) > 200 else ''}\n---"
    if tool_name == "edit_file":
        return (
            f"edit {args.get('path')}\n"
            f"  replace: {args.get('old_str', '')[:150]!r}\n"
            f"  with:    {args.get('new_str', '')[:150]!r}"
        )
    return f"{tool_name}({args})"
 
 
def confirm(tool_name: str, args: dict) -> bool:
    """
    Ask the person to approve a risky tool call. Returns True if approved.
    'a' approves this tool type for the rest of the session (no more asking
    for that tool, though bash commands are still checked against the safe
    prefix list each time).
    """
    print(f"\n\033[91m[confirm]\033[0m Claude wants to {_describe(tool_name, args)}")
    try:
        answer = input("Allow? [y]es / [n]o / [a]lways for this tool: ").strip().lower()
    except EOFError:
        # No TTY to ask on (background/CI) — fail safe: deny, don't crash.
        print("\033[91mNo interactive input available — denying by default.\033[0m")
        return False

    if answer == "a":
        _session_approved.add(tool_name)
        return True
    return answer == "y"
 