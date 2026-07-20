"""Provider abstraction: normalized messages, events, and the Provider interface.

Every concrete provider (Anthropic, OpenAI, Gemini, OpenAI-compatible) converts
these provider-agnostic types to and from its own wire format. The rest of the
app only ever deals with the normalized types defined here.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")


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
        # Tolerates extra keys (e.g. "timestamp" from the .ai/threads
        # interchange format) — they simply don't survive the round-trip.
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


# ---- Shared retry helper ----------------------------------------------------

#: Default policy for provider API calls: 4 total attempts (3 retries) with an
#: exponentially growing, jittered sleep of roughly 0.5s, 1s, 2s between them.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0

# Providers raise ProviderError with the adapters' "HTTP <status>: ..." message
# for HTTP failures, and wrap network errors in ProviderError too — this pulls
# the status code back out so transient HTTP failures can be retried.
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})\b")

#: HTTP statuses worth retrying: rate limit plus any 5xx server error.
#: Every other 4xx is a client error and is never retried.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

NetworkErrorTypes: tuple[type[Exception], ...] = ()
try:  # httpx is an install dependency; the guard only keeps base importable bare.
    import httpx

    NetworkErrorTypes = (httpx.HTTPError,)
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


def error_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status code carried by ``exc``, else ``None``.

    Understands httpx.HTTPStatusError, an ``.status_code``/``.status`` attribute,
    and the "HTTP <status>:" prefix used in ProviderError messages.
    """
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    m = _HTTP_STATUS_RE.search(str(exc))
    return int(m.group(1)) if m else None


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a transient failure worth retrying.

    Retryable: network/transport errors, HTTP 429, and HTTP 5xx.
    Never retryable: any other 4xx client error, and anything unrecognized.
    """
    status = error_status(exc)
    if status is not None:
        return status in RETRYABLE_STATUSES
    return bool(NetworkErrorTypes) and isinstance(exc, NetworkErrorTypes)


def _backoff_delay(
    attempt: int, base_delay: float, max_delay: float, jitter: float
) -> float:
    """Exponential delay (base * 2**attempt, capped) plus random jitter."""
    delay = min(max_delay, base_delay * (2**attempt))
    if jitter > 0:
        delay += random.uniform(0.0, jitter * delay)
    return delay


def retry_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = 0.5,
    is_retryable_fn: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn()`` synchronously, retrying transient failures with backoff.

    ``fn`` is called up to ``max_attempts`` times total (so 1 = no retries).
    Between attempts it sleeps ``base_delay * 2**attempt`` seconds, capped at
    ``max_delay`` and widened by up to ``jitter`` (fraction) of random extra
    delay. Only exceptions for which ``is_retryable_fn`` returns True are
    retried — anything else (4xx client errors other than 429, bugs, ...)
    propagates immediately. When attempts run out, the last exception is
    re-raised. ``sleep`` is injectable so tests don't actually wait.

    Usage (providers build the request fresh per attempt; see T2)::

        def attempt() -> list[Event]:
            return list(self._request(body))  # raises before any partial emit

        events = retry_call(attempt)
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_attempts - 1 or not is_retryable_fn(e):
                raise
            sleep(_backoff_delay(attempt, base_delay, max_delay, jitter))
    raise AssertionError("unreachable")  # pragma: no cover
