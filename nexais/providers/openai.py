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
    default_models = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "o3",
        "o4-mini",
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
        if max_tokens:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        wire_tools = self._tools_to_wire(tools)
        if wire_tools:
            body["tools"] = wire_tools

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
