"""
Planning tool: lets the model externalize a multi-step plan instead of
holding it implicitly in free text. Model passes the FULL list every call,
not a diff, and keeps exactly one item 'in_progress' at a time.

Used to be a module-level global — broke under Phase 5's concurrent
executors (two threads, one shared list). Now a per-instance TodoManager,
one per running agent, so no lock is needed.
"""

import json


class TodoManager:
    """One instance per running agent — never shared across threads, no lock needed."""

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
