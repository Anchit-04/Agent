"""
Model registry — the "API vault" config surface. This is the one file you
touch to add a model: everything upstream (agent.py, and later the router)
refers to models only by the short key below, never by SDK type or raw
model-id string.

To add a model:
  1. Pick a short key (e.g. "deepseek-chat").
  2. Point it at the right Provider class + real model id string.
  3. Name the env var that holds its API key (put the actual key in .env,
     never here).
  4. If it's not Gemini/Anthropic, it's almost certainly OpenAI-compatible —
     reuse OpenAICompatibleProvider with the vendor's base_url.
  5. Set "tier": "strong" or "cheap" — this is what core/routing.py uses to
     pick an orchestrator (strong) vs an executor (cheap) later. It's pure
     routing/vault metadata, not a provider constructor argument — see the
     explicit .pop() in get_provider() below; anything left in `cfg` gets
     spread as **kwargs into the Provider class, so any new registry field
     that isn't a real constructor argument must be popped here too, or
     every get_provider() call breaks with an unexpected-kwarg TypeError.
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
