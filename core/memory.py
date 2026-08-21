"""Shared memory: one markdown file all agents read from and write to."""

import itertools
import threading
from datetime import datetime, timezone

from paths import PROJECT_ROOT

MEMORY_FILE = PROJECT_ROOT / "SHARED_MEMORY.md"

_write_lock = threading.Lock()
_note_counter = itertools.count(1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_entry(entry_id: str, agent: str, content: str, scope: list[str] | None = None, kind: str = "note") -> None:
    scope_str = f" scope={','.join(scope)}" if scope else ""
    block = f"## [{_now()}] id={entry_id} agent={agent} kind={kind}{scope_str}\n{content}\n\n"
    with _write_lock:
        if not MEMORY_FILE.exists():
            MEMORY_FILE.write_text("# Shared Memory\n\n", encoding="utf-8")
        with MEMORY_FILE.open("a", encoding="utf-8") as f:
            f.write(block)


def next_note_id() -> str:
    with _write_lock:
        return f"note_{next(_note_counter)}"


def read_index() -> str:
    """Header lines only — cheap, folded into every agent's system prompt."""
    if not MEMORY_FILE.exists():
        return "(empty)"
    lines = [l for l in MEMORY_FILE.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
    return "\n".join(lines) if lines else "(empty)"


def read_entry(entry_id: str) -> str:
    if not MEMORY_FILE.exists():
        return f"No entries yet (looked for id={entry_id})."
    lines = MEMORY_FILE.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## ") and f"id={entry_id} " in l + " "), None)
    if start is None:
        return f"No entry with id={entry_id!r} found."
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


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
    "memory_write": lambda agent, args: append_entry(next_note_id(), agent, args["content"], kind="note"),
    "memory_read": lambda agent, args: read_entry(args["entry_id"]),
}
