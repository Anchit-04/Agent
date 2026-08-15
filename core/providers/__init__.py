from dotenv import load_dotenv

# Multiple providers means multiple API keys — load .env once here so every
# adapter's os.environ.get(env_key) in base.py just works, instead of each
# entry point having to remember to load it (or relying on the shell having
# them exported, which the previous single-provider version assumed).
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
