"""Durable per-model specialty preferences, folded into the orchestrator's prompt every turn."""

import json
import threading

from paths import FOX_HOME

# FOX_HOME, not the workspace: these preferences describe Fox's own
# models and should follow the installation across projects.
PREFERENCES_FILE = FOX_HOME / "model_preferences.json"

_lock = threading.Lock()


def _load() -> dict[str, str]:
    if not PREFERENCES_FILE.exists():
        return {}
    return json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))


def get_specialty(model_key: str) -> str | None:
    return _load().get(model_key)


def get_all() -> dict[str, str]:
    return _load()


def set_specialty(model_key: str, description: str) -> None:
    with _lock:
        prefs = _load()
        prefs[model_key] = description
        PREFERENCES_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def render_for_prompt() -> str:
    prefs = _load()
    if not prefs:
        return "(none set)"
    return "\n".join(f"- {k}: {v}" for k, v in prefs.items())
