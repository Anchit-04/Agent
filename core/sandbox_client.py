"""
Python-side client for sandboxd — the Go binary (core/sandboxd/main.go) that
actually executes shell commands, with real OS-level containment: a hard
memory ceiling and a guaranteed kill of the entire process tree on timeout,
via a Windows Job Object. See core/sandboxd/main.go's docstring for why this
needed to be a separate process rather than more Python.

Protocol: one JSON request per line into sandboxd's stdin, one JSON response
per line back on its stdout — {"command": "..."} in, {"exit_code": ...,
"output": ...} out. sandboxd is long-lived by design, so we keep exactly one
instance running for the whole agent session instead of spawning a fresh
process per command, restarting it transparently if it ever dies.
"""

import atexit
import json
import subprocess
from pathlib import Path

WORKDIR = "."  # commands run with this as their working directory
MAX_OUTPUT_CHARS = 8000  # cap what goes back to the model — sandboxd itself
                          # doesn't truncate, that's a context-budget concern,
                          # not a sandboxing one, so it lives here instead.

_SANDBOXD_PATH = Path(__file__).parent / "sandboxd" / "sandboxd.exe"

_proc: subprocess.Popen | None = None


def _start() -> subprocess.Popen:
    if not _SANDBOXD_PATH.exists():
        raise RuntimeError(
            f"sandboxd binary not found at {_SANDBOXD_PATH}. Build it first: "
            f"cd core/sandboxd && go build -o sandboxd.exe ."
        )
    proc = subprocess.Popen(
        [str(_SANDBOXD_PATH)],
        cwd=WORKDIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,  # line-buffered, matches the line-per-message protocol
    )
    atexit.register(_stop, proc)
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """Close stdin — sandboxd treats EOF as its normal shutdown signal (see
    main.go's ReadString error handling), so this is a clean exit, not a kill."""
    try:
        proc.stdin.close()
    except Exception:
        pass


def _get_proc() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        # Not started yet, or it's died since the last call — (re)start it.
        _proc = _start()
    return _proc


def run_bash_command(command: str) -> str:
    """
    Execute a shell command inside sandboxd's containment and return its
    result as a JSON string: {"exit_code": int, "output": str}. Same
    signature and return shape as the old subprocess-based implementation,
    so execute_tool() in agent.py didn't need to change at all.

    Per-command timeout/memory overrides aren't wired through yet — every
    request gets sandboxd's fixed defaults (30s / 512MB, see main.go). A
    deliberate fast-follow, not an oversight, same as noted on the Go side.
    """
    proc = _get_proc()
    try:
        proc.stdin.write(json.dumps({"command": command}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    except (BrokenPipeError, OSError) as e:
        return json.dumps({"exit_code": -1, "output": f"sandboxd pipe error: {e}"})

    if not line:
        # sandboxd exited unexpectedly. Don't auto-retry this command —
        # retrying an arbitrary shell command automatically isn't safe if it
        # wasn't idempotent. Report the failure; the next call gets a fresh
        # sandboxd via _get_proc()'s liveness check.
        global _proc
        _proc = None
        return json.dumps({"exit_code": -1, "output": "sandboxd process died unexpectedly"})

    # sandboxd's Response is already our wire format — just enforce the output cap.
    resp = json.loads(line)
    output = resp.get("output", "")
    if len(output) > MAX_OUTPUT_CHARS:
        resp["output"] = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(output)} chars total]"
    return json.dumps(resp)
