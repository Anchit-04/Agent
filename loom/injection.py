"""
Mid-flight human message injection. One queue per session, keyed by task_id
(None = orchestrator/top-level agent, real id = that executor). Drained
between an agent's own turns, never mid-tool-call.

wait_for_message() is the blocking counterpart to drain() — it's what lets a
top-level chat session sit idle, waiting for your next message, instead of
ending the moment the model stops calling tools. close() is the only thing
that wakes a waiter up with nothing to process — an explicit "this session
is over" signal, not a timeout (sessions are meant to sit open for days).
"""

import threading
from collections import defaultdict


class InjectionQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._pending: dict = defaultdict(list)
        self._closed: set = set()

    def push(self, task_id, content: str) -> None:
        with self._condition:
            self._pending[task_id].append(content)
            self._condition.notify_all()

    def drain(self, task_id) -> list:
        """Returns and clears whatever's pending for task_id. Non-blocking —
        an empty list just means nothing arrived, not an error."""
        with self._condition:
            return self._pending.pop(task_id, [])

    def wait_for_message(self, task_id, timeout: float | None = None) -> list[str] | None:
        """Blocks until a message arrives for task_id, close(task_id) is
        called, or timeout elapses. Returns None on close/timeout — the
        caller's cue to stop, not more input to process. timeout=None (the
        default) waits forever, on purpose: a session sitting open for days
        with nobody typing is the whole point, not something to expire."""
        with self._condition:
            while not self._pending.get(task_id) and task_id not in self._closed:
                if not self._condition.wait(timeout=timeout):
                    return None
            if task_id in self._closed:
                return None
            return self._pending.pop(task_id, [])

    def close(self, task_id) -> None:
        """Explicit end-of-session signal — wakes up anyone blocked in
        wait_for_message() with nothing to give them. Never called on
        disconnect; only on a deliberate 'end this session' action."""
        with self._condition:
            self._closed.add(task_id)
            self._condition.notify_all()
