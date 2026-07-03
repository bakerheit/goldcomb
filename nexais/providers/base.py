"""Provider abstraction: normalized messages, events, and the Provider interface.

Every concrete provider (Anthropic, OpenAI, Gemini, OpenAI-compatible) converts
these provider-agnostic types to and from its own wire format. The rest of the
app only ever deals with the normalized types defined here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ToolCall:
    """A tool/function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """A single turn in the conversation, provider-agnostic.

    role is one of: "user", "assistant", "tool". System prompts are passed
    separately to ``Provider.stream`` rather than living in this list.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # For role == "tool": which call this result answers, and the tool name.
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": t.id, "name": t.name, "arguments": t.arguments}
                for t in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content", "") or "",
            tool_calls=[
                ToolCall(id=t["id"], name=t["name"], arguments=t.get("arguments", {}))
                for t in d.get("tool_calls", [])
            ],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )


# ---- Streaming events yielded by Provider.stream ----------------------------


class Event:
    """Base class for streaming events."""


@dataclass
class TextDelta(Event):
    """A chunk of assistant text to display immediately."""

    text: str


@dataclass
class ThinkingDelta(Event):
    """A chunk of the model's reasoning/thinking output (if surfaced)."""

    text: str


@dataclass
class Completed(Event):
    """Terminal event carrying the fully assembled assistant message."""

    message: Message
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """A tool definition the model may call, in JSON-Schema form."""

    name: str
    description: str
    parameters: dict[str, Any]


class ProviderError(RuntimeError):
    """Raised for HTTP / API errors, with a human-readable message."""


class Provider:
    """Base class for all providers.

    Subclasses implement ``stream`` (a generator of Events) and optionally
    ``list_models`` (live model discovery). ``config`` is the per-provider dict
    from the config file, containing at least ``type`` and usually ``api_key``.
    """

    #: Static list of well-known models, used as a hint for completion/`/model list`.
    default_models: list[str] = []

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @property
    def type(self) -> str:
        return self.config.get("type", self.name)

    @property
    def api_key(self) -> str | None:
        return self.config.get("api_key")

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[Event]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Query the provider for available models. Falls back to default_models."""
        return list(self.default_models)


# ---- Shared SSE helper ------------------------------------------------------


def iter_sse(response) -> Iterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from an httpx streaming Server-Sent-Events body.

    ``event`` is the SSE event name (or None), ``data`` is the raw data payload.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    for raw in response.iter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # comment / heartbeat
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def parse_json(data: str) -> Any | None:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
