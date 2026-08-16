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

Every failure mode of that pipe is handled explicitly below (missing
binary, hung sandboxd, malformed response, dead process) — the goal is that
nothing sandboxd does short of the Python process itself dying can crash
the agent loop. Each failure path returns the same {"exit_code", "output"}
error shape a real command failure would, so agent.py never needs to know
the difference.
"""

import atexit
import json
import queue
import subprocess
import threading
from pathlib import Path

WORKDIR = "."  # commands run with this as their working directory
MAX_OUTPUT_CHARS = 8000  # cap what goes back to the model — sandboxd itself
                          # doesn't truncate, that's a context-budget concern,
                          # not a sandboxing one, so it lives here instead.

# How long we'll wait for sandboxd to answer a single command before giving
# up on it. Comfortably above sandboxd's own internal per-command timeout
# (main.go's DefaultTimeout, 30s) so this is a pure backstop — it should only
# ever fire if sandboxd itself is hung (stuck before its own timer even
# starts, or blocked in a Windows API call), not as a race with normal
# completion.
CLIENT_READ_TIMEOUT = 45

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
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """
    Shut sandboxd down. Closing stdin is the polite ask — sandboxd treats
    EOF as its normal shutdown signal (see main.go's ReadString error
    handling) — but that only asks; it doesn't guarantee the process
    actually exits, e.g. if it's stuck mid-command. wait() gives it a few
    seconds to comply on its own; kill() is the guaranteed backstop if it
    doesn't, so we never leak a hung sandboxd.exe when Python exits.
    """
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
    """
    Registered once, for the whole process lifetime — not once per _start()
    call. Always closes over whatever the *current* _proc is at actual exit
    time, so restarting sandboxd partway through a session (it died and
    _get_proc() replaced it) doesn't pile up one dangling atexit handler per
    restart, each pointing at an already-dead Popen object.
    """
    if _proc is not None:
        _stop(_proc)


atexit.register(_cleanup_at_exit)


def _get_proc() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        # Not started yet, or it's died since the last call — (re)start it.
        # _start() can raise RuntimeError (binary not built yet); that's the
        # caller's (run_bash_command's) problem to turn into a tool-error
        # result, not something to catch here.
        _proc = _start()
    return _proc


def _readline_with_timeout(proc: subprocess.Popen, timeout: float) -> str | None:
    """
    proc.stdout.readline() blocks with no way to time it out directly — pipes
    on Windows don't support a clean non-blocking read. Do the read on a
    daemon thread instead and wait on it via a Queue with a timeout. If nothing
    arrives in time we just stop waiting; the reader thread is left blocked on
    the dead/hung process's pipe, which is harmless since it's a daemon
    thread and we're about to kill that process anyway (see run_bash_command).

    Returns the line on success, "" on a clean EOF (mirrors the old
    behavior — readline() itself returns "" at EOF), or None if we timed out
    waiting — a distinct sentinel so callers can tell "sandboxd exited" apart
    from "sandboxd never answered".
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _reader():
        try:
            q.put(proc.stdout.readline())
        except Exception:
            q.put("")  # surfaces the same way as clean EOF to the caller

    threading.Thread(target=_reader, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


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
    global _proc

    try:
        proc = _get_proc()
    except RuntimeError as e:
        # sandboxd.exe isn't built yet — a real, expected state on a fresh
        # checkout. Surface it as a normal tool-error result instead of
        # letting it crash the agent loop on the first bash call.
        return json.dumps({"exit_code": -1, "output": str(e)})

    try:
        proc.stdin.write(json.dumps({"command": command}) + "\n")
        proc.stdin.flush()
        line = _readline_with_timeout(proc, CLIENT_READ_TIMEOUT)
    except (BrokenPipeError, OSError) as e:
        return json.dumps({"exit_code": -1, "output": f"sandboxd pipe error: {e}"})

    if line is None:
        # sandboxd didn't answer within our backstop timeout — it's hung.
        # Its own internal 30s command timeout should have fired and
        # answered well before this, so treat this as sandboxd itself being
        # broken: kill it outright rather than leaving a hung process behind,
        # and don't reuse it — the next call gets a fresh one via _get_proc().
        proc.kill()
        _proc = None
        return json.dumps({
            "exit_code": -1,
            "output": f"sandboxd did not respond within {CLIENT_READ_TIMEOUT}s "
                      "— killed the hung process; it will restart on the next command.",
        })

    if not line:
        # sandboxd exited unexpectedly. Don't auto-retry this command —
        # retrying an arbitrary shell command automatically isn't safe if it
        # wasn't idempotent. Report the failure; the next call gets a fresh
        # sandboxd via _get_proc()'s liveness check.
        _proc = None
        return json.dumps({"exit_code": -1, "output": "sandboxd process died unexpectedly"})

    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        # The response line was malformed/partial rather than clean EOF —
        # most likely sandboxd was killed mid-write. We can no longer trust
        # the one-line-per-message framing is still in sync (a half-written
        # line could leave a stray fragment ahead of the next real
        # response), so don't keep using this process.
        _proc = None
        return json.dumps({"exit_code": -1, "output": f"sandboxd sent a malformed response: {line!r}"})

    # sandboxd's Response is already our wire format — just enforce the output cap.
    output = resp.get("output", "")
    if len(output) > MAX_OUTPUT_CHARS:
        resp["output"] = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(output)} chars total]"
    return json.dumps(resp)
