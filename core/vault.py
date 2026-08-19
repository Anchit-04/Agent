"""
The "vault" side of Phase 4 (API Vault & Routing) — knowing which
registered models actually have a *usable* key, on top of what
providers/config.py's MODEL_REGISTRY only knows in the abstract (what
*should* be configured).

Two distinct questions, kept deliberately separate:
  - "present": is a non-empty value set for this model's env var? Cheap,
    local, no network call.
  - "validated": does that key actually authenticate against the real
    API? A real network round-trip — never run implicitly, only on
    request via validate_key(), and cached per-session (once per process
    run is enough for now; re-checking on every lookup would be wasteful
    and sometimes rate-limited for a fact that rarely changes mid-session).

Keys themselves still live in .env, completely unchanged from before this
module existed. get_key() is the one function in the whole codebase that
knows that — so if a real secret store replaces .env later (cloud
deployment, PLAN.md phase 7), only this function's internals change;
nothing that calls get_key()/available_models() needs to know or care.
"""

import os
from dataclasses import dataclass

from providers.config import MODEL_REGISTRY, get_provider, get_tier


class VaultError(Exception):
    """A requested model's key isn't registered, or isn't present at all."""


@dataclass
class VaultEntry:
    model_key: str
    tier: str
    present: bool
    validated: bool | None  # None = validate_key() hasn't been called for this key this session


# Per-session validation cache: model_key -> bool. Not persisted to disk,
# no TTL — see this module's docstring for why "once per process run" is
# the right granularity for now.
_validated_cache: dict[str, bool] = {}


def get_key(model_key: str) -> str:
    """
    The raw API key value for a registered model. Raises VaultError if the
    key isn't registered at all, or is registered but has nothing set —
    callers (get_provider(), effectively) shouldn't have to know keys
    live in os.environ; this is the one place that does.
    """
    if model_key not in MODEL_REGISTRY:
        raise VaultError(f"Unknown model key {model_key!r}. Known: {sorted(MODEL_REGISTRY)}")
    env_var = MODEL_REGISTRY[model_key]["env_key"]
    value = os.environ.get(env_var)
    if not value:
        raise VaultError(f"No API key set for {model_key!r} — set {env_var} in .env.")
    return value


def is_present(model_key: str) -> bool:
    """Cheap, local, no-network check: is a non-empty value set at all?
    Public (not the presence check's original underscore-prefixed form)
    because core/routing.py needs it too, for the same reason vault.py
    does — routing wants to know "is there anything to try" without
    paying for a real API call on every candidate it considers."""
    if model_key not in MODEL_REGISTRY:
        raise VaultError(f"Unknown model key {model_key!r}. Known: {sorted(MODEL_REGISTRY)}")
    env_var = MODEL_REGISTRY[model_key]["env_key"]
    return bool(os.environ.get(env_var))


def validate_key(model_key: str, force: bool = False) -> bool:
    """
    Confirm model_key's API key actually authenticates, via a real (cheap)
    API call — see Provider.validate() in providers/base.py. Cached per
    session unless force=True re-runs it. Returns False (never raises) if
    the key is missing entirely or the real call fails for any reason —
    callers that need to tell "missing" apart from "present but invalid"
    should check is_present() first, or read validated on a VaultEntry
    from list_models() alongside its present field.
    """
    if not force and model_key in _validated_cache:
        return _validated_cache[model_key]

    if not is_present(model_key):
        _validated_cache[model_key] = False
        return False

    try:
        provider = get_provider(model_key)
        result = provider.validate()
    except Exception:
        result = False

    _validated_cache[model_key] = result
    return result


def list_models() -> list[VaultEntry]:
    """
    Every registered model's current status: tier, whether a key is
    present, and whether it's been validated *this session* (None if
    validate_key() hasn't been called for it yet). Deliberately does NOT
    trigger validation itself — this is a side-effect-free status query;
    validation is an explicit, real-network-call action you opt into.
    """
    return [
        VaultEntry(
            model_key=key,
            tier=get_tier(key),
            present=is_present(key),
            validated=_validated_cache.get(key),
        )
        for key in MODEL_REGISTRY
    ]


def available_models(require_validated: bool = False) -> list[str]:
    """
    model_keys usable right now. require_validated=True additionally
    requires a real API check to have succeeded (running one if it hasn't
    been cached yet) — use this before committing to a model for a
    load-bearing role (see routing.pick_orchestrator()), since discovering
    a present-but-dead key mid-task is worse than a slightly slower
    startup check.
    """
    result = []
    for key in MODEL_REGISTRY:
        if not is_present(key):
            continue
        if require_validated and not validate_key(key):
            continue
        result.append(key)
    return result
