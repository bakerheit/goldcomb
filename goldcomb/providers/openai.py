"""OpenAI Chat Completions provider, and the OpenAI-compatible generic provider.

Any endpoint that speaks the /v1/chat/completions API (OpenRouter, Groq, Together,
Ollama, LM Studio, vLLM, Azure-style gateways, ...) works with OpenAICompatible by
setting a ``base_url``.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .base import (
    Completed,
    Event,
    Message,
    Provider,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolSpec,
    iter_sse,
    parse_json,
)


class OpenAIProvider(Provider):
    type_name = "openai"
    default_base_url = "https://api.openai.com/v1"
    # Curated fallback shown before a live /models fetch (which caches the full
    # real catalog). Not exhaustive — run /models for everything the key can see.
    default_models = [
        "gpt-5",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o",
        "gpt-4o-mini",
        "chatgpt-4o-latest",
        "o3",
        "o4-mini",
        "o1",
    ]

    @property
    def base_url(self) -> str:
        return (self.config.get("base_url") or self.default_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Extra headers (e.g. OpenRouter's HTTP-Referer) can be set in config.
        headers.update(self.config.get("headers", {}))
        return headers

    def _messages_to_wire(
        self, messages: list[Message], system: str | None
    ) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id,
                        "content": m.content,
                    }
                )
            elif m.role == "assistant":
                if not m.content and not m.tool_calls:
                    continue  # nothing to send; empty turns are rejected
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": t.id,
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "arguments": json.dumps(t.arguments),
                            },
                        }
                        for t in m.tool_calls
                    ]
                wire.append(msg)
            else:  # user
                if m.content:
                    wire.append({"role": "user", "content": m.content})
        return wire

    def _tools_to_wire(self, tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _token_param(self, model: str) -> str:
        """Name of the output-token-limit field to send for ``model``.

        OpenAI renamed ``max_tokens`` to ``max_completion_tokens``; the o-series
        and gpt-5+ reasoning models reject the old name outright. The real
        OpenAI API accepts the new name for every current chat model, so default
        to it there. OpenAI-*compatible* gateways (Ollama, vLLM, Groq, ...)
        mostly still expect ``max_tokens``, so default to the old name for them
        and let the self-heal retry cover the exceptions. A per-provider config
        ``token_param`` overrides the choice.
        """
        override = self.config.get("token_param")
        if override in ("max_tokens", "max_completion_tokens"):
            return override
        return "max_completion_tokens" if self.type_name == "openai" else "max_tokens"

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
        body: dict[str, Any] = {
            "model": model,
            "messages": self._messages_to_wire(messages, system),
            "stream": True,
        }
        # Ask the API to emit a final usage chunk while streaming, otherwise no
        # token counts come back and cost reporting is silently blank. A few
        # strict OpenAI-compatible servers reject it — set stream_usage=false.
        if self.config.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
        if max_tokens:
            body[self._token_param(model)] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        # Optional per-provider override (e.g. "none" for reasoning models
        # whose default rejects tools on chat/completions, or "high" to spend
        # more thinking). Unset = let the API decide.
        effort = self.config.get("reasoning_effort")
        if effort:
            body["reasoning_effort"] = str(effort)
        wire_tools = self._tools_to_wire(tools)
        if wire_tools:
            body["tools"] = wire_tools

        # Self-heal the two common OpenAI-family 400s — the max_tokens ->
        # max_completion_tokens rename, and reasoning models rejecting a custom
        # temperature — by adjusting the request and retrying. These errors are
        # raised (via the status check in _request) before any event is yielded,
        # so a retry never double-emits already-streamed text.
        for _ in range(4):
            try:
                yield from self._request(body)
                return
            except ProviderError as e:
                adjusted = _adjust_body_for_error(body, str(e))
                if adjusted is None:
                    raise
                body = adjusted
        raise ProviderError(
            f"{self.name}: could not satisfy the API's parameter requirements"
        )

    def _request(self, body: dict[str, Any]) -> Iterator[Event]:
        text_parts: list[str] = []
        # Accumulate streamed tool-call fragments keyed by their index.
        tool_frags: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                with client.stream("POST", url, headers=self._headers(), json=body) as r:
                    if r.status_code >= 400:
                        raise ProviderError(_http_error(r))
                    for _event, data in iter_sse(r):
                        if data.strip() == "[DONE]":
                            break
                        payload = parse_json(data)
                        if not payload:
                            continue
                        if payload.get("usage"):
                            u = payload["usage"]
                            usage = {
                                "input_tokens": u.get("prompt_tokens", 0),
                                "output_tokens": u.get("completion_tokens", 0),
                            }
                        for choice in payload.get("choices", []):
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                text_parts.append(delta["content"])
                                yield TextDelta(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                frag = tool_frags.setdefault(
                                    idx, {"id": None, "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    frag["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    frag["name"] = fn["name"]
                                if fn.get("arguments"):
                                    frag["arguments"] += fn["arguments"]
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error talking to {self.name}: {e}") from e

        tool_calls = _finalize_tool_calls(tool_frags)
        stop = "tool_use" if tool_calls else _norm_stop(finish_reason)
        yield Completed(
            message=Message(
                role="assistant", content="".join(text_parts), tool_calls=tool_calls
            ),
            stop_reason=stop,
            usage=usage,
        )

    def list_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(url, headers=self._headers())
                if r.status_code >= 400:
                    raise ProviderError(_http_error(r))
                data = r.json()
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error: {e}") from e
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return sorted(ids) or list(self.default_models)


class OpenAICompatibleProvider(OpenAIProvider):
    """OpenAI-compatible endpoint with a required custom base_url."""

    type_name = "openai-compatible"
    default_models: list[str] = []

    @property
    def base_url(self) -> str:
        url = self.config.get("base_url")
        if not url:
            raise ProviderError(
                f"Provider '{self.name}' is openai-compatible but has no base_url. "
                f"Set one with: /provider set {self.name} base_url <url>"
            )
        return url.rstrip("/")


def _finalize_tool_calls(frags: dict[int, dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for idx in sorted(frags):
        f = frags[idx]
        if not f.get("name"):
            continue
        try:
            args = json.loads(f["arguments"]) if f["arguments"].strip() else {}
        except (json.JSONDecodeError, ValueError):
            args = {"_raw": f["arguments"]}
        calls.append(ToolCall(id=f.get("id") or f"call_{idx}", name=f["name"], arguments=args))
    return calls


def _adjust_body_for_error(body: dict[str, Any], message: str) -> dict[str, Any] | None:
    """Return a self-healed copy of ``body`` for a retriable 400, else ``None``.

    Handles the two OpenAI-family parameter mismatches that otherwise crash a
    chat outright:

    * the ``max_tokens`` <-> ``max_completion_tokens`` rename (newer models want
      the new name; a few strict compatible gateways only know the old one), and
    * reasoning models (o-series, gpt-5+) that reject any non-default
      ``temperature``.

    Returns ``None`` when the message isn't one we know how to fix, so the caller
    surfaces the original error unchanged.
    """
    msg = message.lower()
    new = dict(body)
    changed = False

    limit = new.get("max_tokens", new.get("max_completion_tokens"))
    if "max_completion_tokens" in msg and "max_tokens" in new:
        # The old name was rejected in favour of the new one.
        new.pop("max_tokens", None)
        new["max_completion_tokens"] = limit
        changed = True
    elif "max_completion_tokens" in new and "max_completion_tokens" not in msg \
            and "max_tokens" in msg:
        # A stricter compatible endpoint only understands the old name.
        new.pop("max_completion_tokens", None)
        new["max_tokens"] = limit
        changed = True

    if "temperature" in msg and "temperature" in new:
        # Reasoning models accept only the default temperature.
        new.pop("temperature", None)
        changed = True

    if ("reasoning_effort" in msg and "tool" in msg
            and new.get("reasoning_effort") != "none"):
        # Some reasoning models default reasoning_effort server-side and then
        # refuse function tools with it on /v1/chat/completions ("... use
        # /v1/responses or set reasoning_effort to 'none'"). Take the API's
        # own remedy: pin it to 'none' and retry.
        new["reasoning_effort"] = "none"
        changed = True

    return new if changed else None


def _norm_stop(finish_reason: str) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "refusal",
    }.get(finish_reason, finish_reason or "end_turn")


def _http_error(r: httpx.Response) -> str:
    try:
        body = r.read().decode("utf-8", "replace") if not r.is_closed else r.text
    except Exception:  # pragma: no cover - best effort
        body = ""
    detail = body
    parsed = parse_json(body)
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            detail = err.get("message", body)
        elif isinstance(err, str):
            detail = err
        elif parsed.get("message"):
            detail = parsed["message"]
    return f"HTTP {r.status_code}: {detail[:500]}"
