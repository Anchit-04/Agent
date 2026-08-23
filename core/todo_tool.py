"""
Planning tool for mini-agent.

Gives the model a way to externalize a multi-step plan and update it as it
progresses, instead of holding the plan implicitly in free text buried in
its own reasoning. In practice this is the single addition most correlated
with better multi-step task completion — it forces upfront decomposition
and gives the model (and you, watching the terminal) a running anchor to
check progress against.

The model is expected to pass the FULL todo list on every call, not a diff,
and to keep exactly one item 'in_progress' at a time — that constraint is
what keeps it working sequentially instead of context-switching.

State used to be one module-level global (_todos). That was already wrong
before multi-session was ever on the table: Phase 5's own concurrent
executors call run_agent() on separate threads within the SAME session,
and each one calling reset_todos()/todo_write() hit the identical global —
a real data race that predates this fix, just never exercised by a test
that had two executors both call todo_write() at the same time. Fixed by
making todo state a per-instance object (TodoManager), same pattern
ScopeScheduler/DependencyGraph already use — one instance per running
agent, never shared, so no lock is needed here (unlike memory.py's file,
which genuinely is shared across concurrent writers).
"""

import json


class TodoManager:
    """One instance per running agent (single-agent CLI run, one executor,
    or the orchestrator's own plan) — never shared across threads, so this
    needs no lock: only the one loop that owns an instance ever touches it."""

    def __init__(self):
        self._todos: list = []

    def write(self, todos: list) -> str:
        for t in todos:
            if "content" not in t or "status" not in t:
                return json.dumps({"error": "each todo needs 'content' and 'status'"})
            if t["status"] not in ("pending", "in_progress", "completed"):
                return json.dumps({"error": f"invalid status: {t['status']!r}"})

        in_progress_count = sum(1 for t in todos if t["status"] == "in_progress")
        if in_progress_count > 1:
            return json.dumps({
                "error": f"{in_progress_count} items marked in_progress — keep exactly "
                         "one at a time so you work sequentially."
            })

        self._todos = todos
        return json.dumps({"success": True, "count": len(todos)})

    def render(self) -> str:
        """Human-readable checklist for terminal display. Empty string if no plan yet."""
        if not self._todos:
            return ""
        icons = {"pending": "☐", "in_progress": "▶", "completed": "☑"}
        return "\n".join(f"  {icons.get(t['status'], '?')} {t['content']}" for t in self._todos)


TODO_TOOLS = [
    {
        "type": "function",
        "name": "todo_write",
        "description": (
            "Create or update your task list for the current job. Pass the "
            "FULL list of todos every time you call this, not just the ones "
            "that changed. Use it for any task with 3 or more distinct steps: "
            "write the whole plan up front as 'pending' items, then as you "
            "work, mark exactly one item 'in_progress' at a time and flip it "
            "to 'completed' the moment it's actually done — not before, and "
            "not in a batch at the end."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Imperative description of the step, e.g. 'Run the test suite'.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    }
]

TODO_TOOL_HANDLERS = {
    "todo_write": lambda manager, args: manager.write(args["todos"]),
}
