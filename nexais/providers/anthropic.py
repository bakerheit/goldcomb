"""Anthropic Messages API provider (raw HTTP, streaming SSE).

Model IDs and the Messages API shape follow the current Anthropic API:
POST /v1/messages with x-api-key + anthropic-version headers, content-block
streaming, and the tool_use / tool_result block protocol.
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
    ThinkingDelta,
    ToolCall,
    ToolSpec,
    iter_sse,
    parse_json,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    type_name = "anthropic"
    default_base_url = "https://api.anthropic.com"
    default_models = [
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]

    @property
    def base_url(self) -> str:
        return (self.config.get("base_url") or self.default_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.config.get("anthropic_version", ANTHROPIC_VERSION),
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.config.get("headers", {}))
        return headers

    def _messages_to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert normalized messages to Anthropic's content-block format.

        Consecutive tool results are grouped into a single ``user`` turn of
        tool_result blocks, as the API requires.
        """
        wire: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []

        def flush_results() -> None:
            nonlocal pending_results
            if pending_results:
                wire.append({"role": "user", "content": pending_results})
                pending_results = []

        for m in messages:
            if m.role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content,
                    }
                )
                continue
            flush_results()
            if m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for t in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": t.id,
                            "name": t.name,
                            "input": t.arguments,
                        }
                    )
                wire.append({"role": "assistant", "content": blocks or ""})
            else:  # user
                wire.append({"role": "user", "content": m.content})
        flush_results()
        return wire

    def _tools_to_wire(self, tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
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
            "max_tokens": max_tokens or 4096,
            "messages": self._messages_to_wire(messages),
            "stream": True,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        wire_tools = self._tools_to_wire(tools)
        if wire_tools:
            body["tools"] = wire_tools

        text_parts: list[str] = []
        # Track in-progress content blocks by index.
        blocks: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage: dict[str, int] = {}

        url = f"{self.base_url}/v1/messages"
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                with client.stream("POST", url, headers=self._headers(), json=body) as r:
                    if r.status_code >= 400:
                        raise ProviderError(_http_error(r))
                    for event_name, data in iter_sse(r):
                        payload = parse_json(data)
                        if not payload:
                            continue
                        etype = payload.get("type", event_name)
                        if etype == "message_start":
                            u = payload.get("message", {}).get("usage", {})
                            usage["input_tokens"] = u.get("input_tokens", 0)
                        elif etype == "content_block_start":
                            idx = payload["index"]
                            cb = payload.get("content_block", {})
                            blocks[idx] = {
                                "type": cb.get("type"),
                                "id": cb.get("id"),
                                "name": cb.get("name"),
                                "json": "",
                            }
                        elif etype == "content_block_delta":
                            idx = payload["index"]
                            delta = payload.get("delta", {})
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                text_parts.append(delta["text"])
                                yield TextDelta(delta["text"])
                            elif dtype == "thinking_delta":
                                yield ThinkingDelta(delta.get("thinking", ""))
                            elif dtype == "input_json_delta":
                                blocks.setdefault(idx, {"json": ""})
                                blocks[idx]["json"] += delta.get("partial_json", "")
                        elif etype == "message_delta":
                            d = payload.get("delta", {})
                            if d.get("stop_reason"):
                                stop_reason = d["stop_reason"]
                            u = payload.get("usage", {})
                            if u.get("output_tokens") is not None:
                                usage["output_tokens"] = u["output_tokens"]
                        elif etype == "error":
                            err = payload.get("error", {})
                            raise ProviderError(
                                f"API error: {err.get('message', json.dumps(err))}"
                            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error talking to {self.name}: {e}") from e

        tool_calls: list[ToolCall] = []
        for idx in sorted(blocks):
            b = blocks[idx]
            if b.get("type") == "tool_use":
                raw = b.get("json", "")
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"_raw": raw}
                tool_calls.append(
                    ToolCall(id=b.get("id") or f"toolu_{idx}", name=b.get("name") or "", arguments=args)
                )

        yield Completed(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls),
            stop_reason=stop_reason,
            usage=usage,
        )

    def list_models(self) -> list[str]:
        url = f"{self.base_url}/v1/models"
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(url, headers=self._headers())
                if r.status_code >= 400:
                    raise ProviderError(_http_error(r))
                data = r.json()
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error: {e}") from e
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return ids or list(self.default_models)


def _http_error(r: httpx.Response) -> str:
    try:
        body = r.read().decode("utf-8", "replace") if not r.is_closed else r.text
    except Exception:  # pragma: no cover
        body = ""
    detail = body
    parsed = parse_json(body)
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            detail = err.get("message", body)
    return f"HTTP {r.status_code}: {detail[:500]}"
