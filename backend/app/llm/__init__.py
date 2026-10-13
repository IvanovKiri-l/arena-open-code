"""LLM-клиенты арены."""

from .base import ChunkHandler, Completion, LLMClient
from .factory import build_client
from .fake import FakeLLMClient
from .openai_compat import OpenAICompatClient, ProviderError

__all__ = [
    "ChunkHandler",
    "Completion",
    "FakeLLMClient",
    "LLMClient",
    "OpenAICompatClient",
    "ProviderError",
    "build_client",
]
