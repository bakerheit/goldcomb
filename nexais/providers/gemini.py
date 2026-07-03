"""Google Gemini provider (generativelanguage API, streaming SSE)."""

from __future__ import annotations

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


class GeminiProvider(Provider):
    type_name = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    default_models = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    @property
    def base_url(self) -> str:
        return (self.config.get("base_url") or self.default_base_url).rstrip("/")

    def _params(self) -> dict[str, str]:
        return {"key": self.api_key} if self.api_key else {}

    def _messages_to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        pending_fn: list[dict[str, Any]] = []

        def flush_fn() -> None:
            nonlocal pending_fn
            if pending_fn:
                contents.append({"role": "user", "parts": pending_fn})
                pending_fn = []

        for m in messages:
            if m.role == "tool":
                pending_fn.append(
                    {
                        "functionResponse": {
                            "name": m.name or "tool",
                            "response": {"result": m.content},
                        }
                    }
                )
                continue
            flush_fn()
            if m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                for t in m.tool_calls:
                    parts.append({"functionCall": {"name": t.name, "args": t.arguments}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:  # user
                contents.append({"role": "user", "parts": [{"text": m.content}]})
        flush_fn()
        return contents

    def _tools_to_wire(self, tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "functionDeclarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": _clean_schema(t.parameters),
                    }
                    for t in tools
                ]
            }
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
        gen_config: dict[str, Any] = {}
        if max_tokens:
            gen_config["maxOutputTokens"] = max_tokens
        if temperature is not None:
            gen_config["temperature"] = temperature

        body: dict[str, Any] = {"contents": self._messages_to_wire(messages)}
        if gen_config:
            body["generationConfig"] = gen_config
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        wire_tools = self._tools_to_wire(tools)
        if wire_tools:
            body["tools"] = wire_tools

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        stop_reason = "end_turn"
        usage: dict[str, int] = {}

        model_path = model if model.startswith("models/") else f"models/{model}"
        url = f"{self.base_url}/{model_path}:streamGenerateContent"
        params = {**self._params(), "alt": "sse"}
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                with client.stream(
                    "POST", url, params=params, json=body,
                    headers={"content-type": "application/json"},
                ) as r:
                    if r.status_code >= 400:
                        raise ProviderError(_http_error(r))
                    for _event, data in iter_sse(r):
                        payload = parse_json(data)
                        if not payload:
                            continue
                        if payload.get("usageMetadata"):
                            u = payload["usageMetadata"]
                            usage = {
                                "input_tokens": u.get("promptTokenCount", 0),
                                "output_tokens": u.get("candidatesTokenCount", 0),
                            }
                        for cand in payload.get("candidates", []):
                            for part in cand.get("content", {}).get("parts", []):
                                if "text" in part and part["text"]:
                                    text_parts.append(part["text"])
                                    yield TextDelta(part["text"])
                                elif "functionCall" in part:
                                    fc = part["functionCall"]
                                    tool_calls.append(
                                        ToolCall(
                                            id=f"call_{len(tool_calls)}",
                                            name=fc.get("name", ""),
                                            arguments=fc.get("args", {}) or {},
                                        )
                                    )
                            if cand.get("finishReason"):
                                stop_reason = _norm_stop(cand["finishReason"])
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error talking to {self.name}: {e}") from e

        if tool_calls:
            stop_reason = "tool_use"
        yield Completed(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls),
            stop_reason=stop_reason,
            usage=usage,
        )

    def list_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(url, params=self._params())
                if r.status_code >= 400:
                    raise ProviderError(_http_error(r))
                data = r.json()
        except httpx.HTTPError as e:
            raise ProviderError(f"Network error: {e}") from e
        ids = []
        for m in data.get("models", []):
            name = m.get("name", "")
            methods = m.get("supportedGenerationMethods", [])
            if methods and "generateContent" not in methods:
                continue
            ids.append(name.replace("models/", ""))
        return sorted(ids) or list(self.default_models)


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini rejects some JSON-Schema keywords; strip the ones it dislikes."""
    if not isinstance(schema, dict):
        return schema
    drop = {"additionalProperties", "$schema", "title", "default", "examples"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in drop:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _clean_schema(v)
        else:
            out[k] = v
    return out


def _norm_stop(reason: str) -> str:
    return {
        "STOP": "end_turn",
        "MAX_TOKENS": "max_tokens",
        "SAFETY": "refusal",
        "RECITATION": "refusal",
    }.get(reason, reason or "end_turn")


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
