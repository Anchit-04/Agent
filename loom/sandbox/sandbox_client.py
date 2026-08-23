"""
Python-side client for sandboxd (loom/sandbox/sandboxd/main.go) — the Go
binary that actually executes shell commands, with real OS-level
containment via a Windows Job Object (hard memory ceiling, guaranteed
process-tree kill on timeout).

Protocol: one JSON request per line into sandboxd's stdin, one JSON response
per line back on stdout. sandboxd is long-lived — we keep one instance
running for the whole session instead of spawning a process per command,
restarting it transparently if it dies.
"""

import atexit
import json
import queue
import subprocess
import threading
from pathlib import Path

from paths import PROJECT_ROOT

WORKDIR = PROJECT_ROOT  # commands run with this as their working directory — see paths.py
MAX_OUTPUT_CHARS = 8000  # context-budget cap, not a sandboxing concern — sandboxd itself doesn't truncate

# Comfortably above sandboxd's own 30s per-command timeout (main.go's
# DefaultTimeout) — this should only fire if sandboxd itself is hung.
CLIENT_READ_TIMEOUT = 45

_SANDBOXD_PATH = Path(__file__).parent / "sandboxd" / "sandboxd.exe"

_proc: subprocess.Popen | None = None


def _start() -> subprocess.Popen:
    if not _SANDBOXD_PATH.exists():
        raise RuntimeError(
            f"sandboxd binary not found at {_SANDBOXD_PATH}. Build it first: "
            f"cd loom/sandbox/sandboxd && go build -o sandboxd.exe ."
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
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """Closing stdin asks sandboxd to exit cleanly (its EOF signal); wait()
    gives it a few seconds, kill() is the guaranteed backstop if it's stuck."""
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _cleanup_at_exit() -> None:
    """Registered once for the process lifetime — always closes over whatever
    _proc currently is, so a mid-session restart doesn't leak old handlers."""
    if _proc is not None:
        _stop(_proc)


atexit.register(_cleanup_at_exit)


def _get_proc() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = _start()  # not started yet, or died since last call
    return _proc


def _readline_with_timeout(proc: subprocess.Popen, timeout: float) -> str | None:
    """proc.stdout.readline() can't be timed out directly on Windows pipes,
    so read on a daemon thread and wait on a Queue instead. Returns the line,
    "" on clean EOF, or None if we gave up waiting."""
    q: queue.Queue = queue.Queue(maxsize=1)

    def _reader():
        try:
            q.put(proc.stdout.readline())
        except Exception:
            q.put("")

    threading.Thread(target=_reader, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def run_bash_command(command: str) -> str:
    """Execute a shell command inside sandboxd's containment. Returns
    {"exit_code": int, "output": str} as a JSON string — same shape the
    old subprocess-based implementation returned, so callers didn't change.
    Per-command timeout/memory overrides aren't wired through yet."""
    global _proc

    try:
        proc = _get_proc()
    except RuntimeError as e:
        # sandboxd.exe isn't built yet — expected on a fresh checkout.
        return json.dumps({"exit_code": -1, "output": str(e)})

    try:
        proc.stdin.write(json.dumps({"command": command}) + "\n")
        proc.stdin.flush()
        line = _readline_with_timeout(proc, CLIENT_READ_TIMEOUT)
    except (BrokenPipeError, OSError) as e:
        return json.dumps({"exit_code": -1, "output": f"sandboxd pipe error: {e}"})

    if line is None:
        # Hung — sandboxd's own 30s timeout should've answered by now. Kill
        # it and don't reuse it; the next call gets a fresh one.
        proc.kill()
        _proc = None
        return json.dumps({
            "exit_code": -1,
            "output": f"sandboxd did not respond within {CLIENT_READ_TIMEOUT}s "
                      "— killed the hung process; it will restart on the next command.",
        })

    if not line:
        # Exited unexpectedly. Don't auto-retry — the command might not be idempotent.
        _proc = None
        return json.dumps({"exit_code": -1, "output": "sandboxd process died unexpectedly"})

    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        # Malformed line, likely killed mid-write — framing may be out of
        # sync now, so don't trust this process for the next call either.
        _proc = None
        return json.dumps({"exit_code": -1, "output": f"sandboxd sent a malformed response: {line!r}"})

    output = resp.get("output", "")
    if len(output) > MAX_OUTPUT_CHARS:
        resp["output"] = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(output)} chars total]"
    return json.dumps(resp)
