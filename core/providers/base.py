"""
Provider-agnostic types for the agent loop.

Every adapter in this package (gemini.py, anthropic.py, openai_compatible.py)
translates between these neutral shapes and its own SDK's wire format. The
agent loop (agent.py) only ever imports from here — it never touches a
provider SDK directly. That's what makes swapping which model answers a
config change (providers/config.py) instead of a rewrite, and is the
precondition for routing different steps of the same task to different
models later.

Design note: history is kept as a flat list of Turn objects, not each
provider's native message shape. A Turn only ever carries plain data (ids,
names, dicts, strings), so any provider can rebuild its own wire format from
scratch on every call — nothing provider-native is ever stored, so there's
no risk of one adapter's turn objects leaking into another's request.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os


@dataclass
class ToolCall:
    id: str          # provider-issued call id; adapters synthesize one if their SDK doesn't hand back an id (Gemini)
    name: str
    args: dict
    raw: dict | None = None  # opaque provider-private extra state (e.g. Gemini's thought_signature).
                              # Only the adapter that set it ever reads it back — never inspect this
                              # from agent.py or another provider's adapter.


@dataclass
class ToolResult:
    call_id: str      # must match the ToolCall.id it answers
    name: str
    content: str


@dataclass
class Turn:
    """One entry in the conversation history."""
    role: str                                       # "user" | "assistant"
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)     # assistant turn requesting tools
    tool_results: list[ToolResult] = field(default_factory=list)  # user turn carrying results back


@dataclass
class ProviderResponse:
    text: str | None
    tool_calls: list[ToolCall]


class Provider(ABC):
    """
    One LLM backend. Stateless per call: takes the full neutral history in,
    returns the model's next turn out. Doesn't retain conversation state
    itself — agent.py owns that.
    """

    def __init__(self, model_id: str, env_key: str, base_url: str | None = None):
        self.model_id = model_id
        self.api_key = os.environ.get(env_key)
        if not self.api_key:
            raise RuntimeError(
                f"Missing API key: set the {env_key} environment variable "
                f"(e.g. in .env) to use model_id={model_id!r}."
            )
        self.base_url = base_url

    @abstractmethod
    def generate(self, history: list[Turn], tools: list[dict], system_prompt: str) -> ProviderResponse:
        """Send the full history + provider-agnostic tool schemas, get the model's next turn back."""
        ...

    def validate(self) -> bool:
        """
        Confirm this provider's API key actually authenticates, via the
        cheapest real call every SDK wired up here happens to support:
        listing available models. No completion request, no token spend —
        just confirms the key is real and not revoked/wrong-scoped, which
        "is the env var non-empty" (core/vault.py's presence check) can't
        tell you. Not an abstractmethod: every adapter's self._client
        (anthropic.Anthropic / genai.Client / openai.OpenAI) exposes
        .models.list() identically, so one implementation covers all three
        without touching them — override in a subclass if a future
        provider's SDK doesn't share that shape.

        Returns False rather than raising on any failure — this method's
        only job is answering "does this key work right now", not
        surfacing why; core/vault.py decides what to do with the result.
        """
        try:
            self._client.models.list()
            return True
        except Exception:
            return False
