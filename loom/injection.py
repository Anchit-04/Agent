"""
Mid-flight human message injection. One queue per session, keyed by task_id
(None = orchestrator, real id = that executor). Drained between an agent's
own turns, never mid-tool-call.
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
