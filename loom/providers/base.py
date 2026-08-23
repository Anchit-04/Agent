"""
Provider-agnostic types for the agent loop. Each adapter (gemini.py,
anthropic.py, openai_compatible.py) translates between these neutral shapes
and its own SDK's wire format — agent.py never touches a provider SDK
directly, so swapping models is a config change, not a rewrite.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os


@dataclass
class ToolCall:
    id: str          # provider-issued call id; adapters synthesize one if their SDK doesn't hand back an id (Gemini)
    name: str
    args: dict
    raw: dict | None = None  # opaque provider-private state (e.g. Gemini's thought_signature) — only that adapter reads it back


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
        """Confirms the key actually authenticates, via the cheapest call
        every SDK here supports: listing models. Not abstract since every
        adapter's client exposes .models.list() the same way. Never raises."""
        try:
            self._client.models.list()
            return True
        except Exception:
            return False
