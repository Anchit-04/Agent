from dotenv import load_dotenv

# Load .env once here so every adapter's os.environ.get(env_key) just works.
load_dotenv()

from .base import ProviderResponse, Provider, Turn, ToolCall, ToolResult
from .config import MODEL_REGISTRY, get_provider

__all__ = [
    "Provider",
    "ProviderResponse",
    "Turn",
    "ToolCall",
    "ToolResult",
    "MODEL_REGISTRY",
    "get_provider",
]
