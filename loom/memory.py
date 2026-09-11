"""
Shared memory: one markdown file all agents in a session read from and write to.

Used to be a module-level global — fine for one session, but two sessions
would've silently shared the same log. Now a per-instance class, one Memory
per session; default_memory() is only for the standalone single-agent CLI.
"""

import itertools
import threading
from datetime import datetime, timezone
from pathlib import Path

import paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memory:
    """One instance per session. Own file, own lock, own note counter —
    no state shared with any other session's Memory instance."""

    def __init__(self, memory_file: Path):
        self.memory_file = memory_file
        self._write_lock = threading.Lock()
        self._note_counter = itertools.count(1)

    def append_entry(self, entry_id: str, agent: str, content: str,
                      scope: list[str] | None = None, kind: str = "note") -> None:
        scope_str = f" scope={','.join(scope)}" if scope else ""
        block = f"## [{_now()}] id={entry_id} agent={agent} kind={kind}{scope_str}\n{content}\n\n"
        with self._write_lock:
            if not self.memory_file.exists():
                self.memory_file.parent.mkdir(parents=True, exist_ok=True)
                self.memory_file.write_text("# Shared Memory\n\n", encoding="utf-8")
            with self.memory_file.open("a", encoding="utf-8") as f:
                f.write(block)

    def next_note_id(self) -> str:
        with self._write_lock:
            return f"note_{next(self._note_counter)}"

    def read_index(self) -> str:
        """Header lines only — cheap, folded into every agent's system prompt."""
        if not self.memory_file.exists():
            return "(empty)"
        lines = [l for l in self.memory_file.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
        return "\n".join(lines) if lines else "(empty)"

    def read_entry(self, entry_id: str) -> str:
        if not self.memory_file.exists():
            return f"No entries yet (looked for id={entry_id})."
        lines = self.memory_file.read_text(encoding="utf-8").splitlines()
        start = next((i for i, l in enumerate(lines) if l.startswith("## ") and f"id={entry_id} " in l + " "), None)
        if start is None:
            return f"No entry with id={entry_id!r} found."
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        return "\n".join(lines[start:end]).strip()


_default: Memory | None = None


def default_memory() -> Memory:
    """Fallback Memory for callers that didn't bring their own — the standalone
    CLI, mostly; a real session always passes its own instance.

    Built on first use rather than at import, because the workspace isn't
    chosen until startup and this log lives inside it. Cached afterwards so
    one process keeps one log per run.
    """
    global _default
    if _default is None:
        _default = Memory(paths.shared_memory_file())
    return _default


MEMORY_WRITE_TOOL = {
    "type": "function",
    "name": "memory_write",
    "description": "Leave a note in shared memory for other agents to read. Task results are logged automatically; use this for extra context.",
    "parameters": {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "The note."}},
        "required": ["content"],
    },
}

MEMORY_READ_TOOL = {
    "type": "function",
    "name": "memory_read",
    "description": "Read a full shared memory entry by id (ids are shown in the index in your system prompt).",
    "parameters": {
        "type": "object",
        "properties": {"entry_id": {"type": "string", "description": "Entry id, exactly as shown in the index."}},
        "required": ["entry_id"],
    },
}

MEMORY_TOOLS = [MEMORY_WRITE_TOOL, MEMORY_READ_TOOL]
MEMORY_TOOL_HANDLERS = {
    "memory_write": lambda mem, agent, args: mem.append_entry(mem.next_note_id(), agent, args["content"], kind="note"),
    "memory_read": lambda mem, agent, args: mem.read_entry(args["entry_id"]),
}
