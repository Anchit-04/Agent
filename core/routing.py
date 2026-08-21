"""
The "routing" side of Phase 4 — turning a *role* ("orchestrator" or
"executor") into a model_key, using vault.py's availability + each
model's tier ("strong" | "cheap", set in providers/config.py's
MODEL_REGISTRY).

This is deliberately role-based, not task-based: the product vision
(PLAN.md) is "strongest model plans and directs, cheaper models execute",
not "route this specific task to whichever model is best suited to it".
That second, fancier kind of routing has no orchestrator built yet to make
the call — this module is the exact seam phase 5's orchestrator will call
into once it exists, kept intentionally simple until there's a real
consumer to design the fancier version against.

Two roles, two different failure philosophies, both explained where they
matter below:
  - pick_orchestrator(): fails loud. The orchestrator is the load-bearing
    "brain" of the whole multi-agent setup — silently substituting a
    weaker model here would defeat the point in a way that's hard to
    notice until something subtly goes wrong.
  - pick_executor()/pick_executors(): degrade gracefully where reasonable.
    An executor is more expendable — "something usable, even suboptimal"
    beats "nothing" for actually getting work done during early
    development, when most setups won't have all 4 registry keys filled in.
"""

from providers.config import MODEL_REGISTRY, get_tier
import vault


class RoutingError(Exception):
    """No model satisfies the requested role, given what's currently in the vault."""


def pick_orchestrator() -> str:
    """
    The strongest available model, for the orchestrator role. Requires a
    real *validated* key (not just present) — see this module's docstring
    for why the orchestrator gets the strict treatment. Raises
    RoutingError rather than falling back to a cheap model if no
    strong-tier key validates.
    """
    strong_keys = [k for k in MODEL_REGISTRY if get_tier(k) == "strong"]
    for key in strong_keys:
        if vault.validate_key(key):
            return key
    raise RoutingError(
        f"No 'strong' tier model has a working API key — the orchestrator "
        f"role needs one of {strong_keys} configured and valid. Set the "
        "corresponding key in .env."
    )


def pick_executor(exclude: frozenset[str] = frozenset(), preferred: str | None = None) -> str:
    """
    A model for the executor role, not already in `exclude` (so
    pick_executors() below can assemble several distinct ones). Prefers a
    cheap-tier model; only checks presence for the cheap-tier candidates,
    not full validation — executors are less failure-sensitive than the
    orchestrator (see module docstring), so a present-but-dead key can be
    discovered and swapped when it actually fails a real call, rather than
    paying for a validation round-trip on every candidate up front.

    If no cheap-tier key is present at all, falls back to any present key
    regardless of tier (even the orchestrator's strong-tier model) — an
    expensive executor beats no executor for early development, where most
    setups won't have every registry slot filled in.

    `preferred`: an explicit model request (e.g. "use kimi-k2 for this
    frontend task"). Unlike the generic fallback above, this fails loud if
    it can't be honored — an explicit choice had a reason, so silently
    substituting a different model would defeat it.
    """
    if preferred is not None:
        if preferred in exclude:
            raise RoutingError(f"requested executor {preferred!r} already used in this batch")
        if not vault.is_present(preferred):
            raise RoutingError(f"requested executor {preferred!r} has no API key configured")
        return preferred

    cheap_candidates = [
        k for k in MODEL_REGISTRY if get_tier(k) == "cheap" and k not in exclude and vault.is_present(k)
    ]
    if cheap_candidates:
        return cheap_candidates[0]

    fallback_candidates = [k for k in MODEL_REGISTRY if k not in exclude and vault.is_present(k)]
    if fallback_candidates:
        return fallback_candidates[0]

    raise RoutingError(
        f"No model with a present API key is available for an executor "
        f"(excluding {sorted(exclude)}). Configure at least one key in .env."
    )


def pick_executors(n: int) -> list[str]:
    """
    n distinct executor model_keys. Raises RoutingError if fewer than n
    are actually available — silently duplicating a model (reusing the
    same key twice) would defeat the point of asking for n *distinct*
    executors, so this fails loud rather than quietly returning fewer.
    """
    picked: list[str] = []
    for _ in range(n):
        try:
            picked.append(pick_executor(exclude=frozenset(picked)))
        except RoutingError:
            raise RoutingError(
                f"Only {len(picked)} distinct executor(s) available, need {n}. "
                "Configure more API keys in .env."
            )
    return picked
