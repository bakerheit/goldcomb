"""Provider registry — maps a provider ``type`` string to its implementation."""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import (
    Completed,
    Event,
    Message,
    Provider,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolSpec,
)
from .gemini import GeminiProvider
from .openai import OpenAICompatibleProvider, OpenAIProvider

# type string -> Provider subclass
PROVIDER_TYPES: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "gemini": GeminiProvider,
}

# Friendly aliases accepted from the CLI.
TYPE_ALIASES = {
    "claude": "anthropic",
    "gpt": "openai",
    "oai": "openai",
    "google": "gemini",
    "compat": "openai-compatible",
    "compatible": "openai-compatible",
    "openrouter": "openai-compatible",
    "groq": "openai-compatible",
    "ollama": "openai-compatible",
    "together": "openai-compatible",
}


def normalize_type(type_str: str) -> str:
    t = type_str.strip().lower()
    return TYPE_ALIASES.get(t, t)


def build_provider(name: str, config: dict[str, Any]) -> Provider:
    ptype = normalize_type(config.get("type", ""))
    cls = PROVIDER_TYPES.get(ptype)
    if cls is None:
        raise ProviderError(
            f"Unknown provider type '{config.get('type')}'. "
            f"Known types: {', '.join(sorted(PROVIDER_TYPES))}"
        )
    return cls(name, {**config, "type": ptype})


def default_models_for(type_str: str) -> list[str]:
    cls = PROVIDER_TYPES.get(normalize_type(type_str))
    return list(getattr(cls, "default_models", [])) if cls else []


__all__ = [
    "PROVIDER_TYPES",
    "build_provider",
    "normalize_type",
    "default_models_for",
    "Provider",
    "ProviderError",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Event",
    "TextDelta",
    "ThinkingDelta",
    "Completed",
]
