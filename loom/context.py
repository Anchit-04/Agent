"""
Context management: nothing truncates the growing turn history on its own,
so we periodically replace it with a compact summary instead. Safe because
todo_write already externalizes the one thing that matters — progress.

COMPACT_EVERY: soft reset (smaller context, task keeps going).
MAX_ITERATIONS: hard stop, so a looping agent can't run forever.
"""

MAX_ITERATIONS = 30
COMPACT_EVERY = 12


def build_compact_input(original_task: str, todo_checklist: str, recent_results: list) -> str:
    """
    Construct a fresh starting prompt that replaces a long interaction chain.
    Must include the just-computed tool results the model is waiting on —
    otherwise it won't know whether its last action succeeded.
    """
    parts = [f"Original task: {original_task}"]

    if todo_checklist:
        parts.append(f"\nYour plan so far:\n{todo_checklist}")
        parts.append(
            "\n(Context was compacted to stay within limits — this is a fresh "
            "conversation, but your plan above reflects real progress. Do not "
            "redo steps already marked completed.)"
        )
    else:
        parts.append(
            "\n(Context was compacted to stay within limits — no plan was "
            "recorded yet. Use todo_write going forward, and check current "
            "file state before continuing since you may have partial progress.)"
        )

    if recent_results:
        parts.append("\nResults of your most recent actions, before this reset:")
        for r in recent_results:
            parts.append(f"- {r['name']}: {r['content'][:300]}")

    parts.append("\nContinue the task from here.")
    return "\n".join(parts)