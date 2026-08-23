"""
Mid-flight human message injection. One InjectionQueue per session, shared
by the orchestrator's own loop and every executor it spawns — keyed by
task_id (None = the orchestrator itself, a real id = that specific
executor), matching the same task_id convention used everywhere else.

Pushed to by server.py's WebSocket handler thread when a human_message
arrives; drained by whichever loop owns that task_id, between its own
turns — never mid-tool-call, so an in-flight tool round-trip is never
corrupted by a message landing halfway through it.
"""

import threading
from collections import defaultdict


class InjectionQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict = defaultdict(list)

    def push(self, task_id, content: str) -> None:
        with self._lock:
            self._pending[task_id].append(content)

    def drain(self, task_id) -> list:
        """Returns and clears whatever's pending for task_id. Non-blocking —
        an empty list just means nothing arrived, not an error."""
        with self._lock:
            return self._pending.pop(task_id, [])
