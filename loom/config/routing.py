

from providers.config import MODEL_REGISTRY, get_tier
from . import vault


class RoutingError(Exception):
    """No model satisfies the requested role, given what's currently in the vault."""


def pick_orchestrator() -> str:
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
