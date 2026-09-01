
import os
from dataclasses import dataclass

import dotenv

from paths import PROJECT_ROOT
from providers.config import MODEL_REGISTRY, get_provider, get_tier


class VaultError(Exception):
    """A requested model's key isn't registered, or isn't present at all."""


@dataclass
class VaultEntry:
    model_key: str
    tier: str
    present: bool
    validated: bool | None  # None = validate_key() hasn't been called for this key this session


# Per-session validation cache, not persisted, no TTL.
_validated_cache: dict[str, bool] = {}


def get_key(model_key: str) -> str:
    if model_key not in MODEL_REGISTRY:
        raise VaultError(f"Unknown model key {model_key!r}. Known: {sorted(MODEL_REGISTRY)}")
    env_var = MODEL_REGISTRY[model_key]["env_key"]
    value = os.environ.get(env_var)
    if not value:
        raise VaultError(f"No API key set for {model_key!r} — set {env_var} in .env.")
    return value


def set_key(model_key: str, value: str) -> None:
    """Writes an API key into .env for a registered model, via python-dotenv's
    own set_key() so the file's existing entries/formatting are preserved.
    Also updates the live process env and drops any stale cached validation
    result, so the key is usable immediately without a restart."""
    if model_key not in MODEL_REGISTRY:
        raise VaultError(f"Unknown model key {model_key!r}. Known: {sorted(MODEL_REGISTRY)}")
    env_var = MODEL_REGISTRY[model_key]["env_key"]
    dotenv.set_key(str(PROJECT_ROOT / ".env"), env_var, value)
    os.environ[env_var] = value
    _validated_cache.pop(model_key, None)


def is_present(model_key: str) -> bool:
    if model_key not in MODEL_REGISTRY:
        raise VaultError(f"Unknown model key {model_key!r}. Known: {sorted(MODEL_REGISTRY)}")
    env_var = MODEL_REGISTRY[model_key]["env_key"]
    return bool(os.environ.get(env_var))


def validate_key(model_key: str, force: bool = False) -> bool:
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
    result = []
    for key in MODEL_REGISTRY:
        if not is_present(key):
            continue
        if require_validated and not validate_key(key):
            continue
        result.append(key)
    return result
