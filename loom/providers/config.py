"""
Model registry — the one file you touch to add a model. Everything upstream
refers to models only by the short key below, never SDK type or raw model id.

To add one: pick a key, point it at a Provider class + real model id, name
the env var holding its key (actual key goes in .env, never here). Not
Gemini/Anthropic? It's almost certainly OpenAI-compatible. "tier" is pure
routing metadata, not a constructor arg — must be popped in get_provider()
below or every call breaks with an unexpected-kwarg TypeError.
"""

from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider

MODEL_REGISTRY = {
    "gemini-flash": {
        "provider": GeminiProvider,
        "model_id": "gemini-3.6-flash",
        "env_key": "GEMINI_API_KEY",
        "tier": "cheap",
    },
    "claude-opus": {
        "provider": AnthropicProvider,
        "model_id": "claude-opus-5",
        "env_key": "ANTHROPIC_API_KEY",
        "tier": "strong",
    },
    "deepseek-chat": {
        "provider": OpenAICompatibleProvider,
        "model_id": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "tier": "cheap",
    },
    "kimi-k2": {
        "provider": OpenAICompatibleProvider,
        "model_id": "kimi-k2-0711-preview",
        "env_key": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.ai/v1",
        "tier": "cheap",
    },
}


def get_provider(key: str):
    """Instantiate the Provider for a registry key. Raises if the key is
    unknown, or if that provider's API key env var isn't set."""
    if key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key {key!r}. Known: {sorted(MODEL_REGISTRY)}")
    cfg = dict(MODEL_REGISTRY[key])
    provider_cls = cfg.pop("provider")
    model_id = cfg.pop("model_id")
    cfg.pop("tier", None)  # routing/vault metadata, not a Provider constructor arg
    return provider_cls(model_id=model_id, **cfg)


def get_tier(key: str) -> str:
    """The routing tier for a registry key."""
    if key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key {key!r}. Known: {sorted(MODEL_REGISTRY)}")
    return MODEL_REGISTRY[key]["tier"]
