"""
Context management for mini-agent.

The Gemini Interactions API manages conversation state server-side via
previous_interaction_id — you never see or truncate the message list
yourself, unlike the Anthropic version of this harness. That's convenient,
but it also means the underlying context keeps growing every single turn
with no way to selectively drop old, no-longer-relevant tool results. A long
task will eventually hit the model's context window regardless.

This module implements the same fix real harnesses use: periodically
abandon the interaction chain and start a fresh one from a compact summary,
instead of an ever-growing history. This works cheaply here because the
todo_write tool already externalizes the one thing that actually matters —
progress and remaining steps — so nothing essential is lost by dropping the
raw turn-by-turn history.

Two independent limits:
  - COMPACT_EVERY : after this many turns, reset the chain (soft limit,
                     the agent keeps working, just with a smaller context)
  - MAX_ITERATIONS : hard stop — abandon the task entirely past this many
                     turns, so a confused/looping agent can't run forever
                     (or burn through your free-tier quota unattended)
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