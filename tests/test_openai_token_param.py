"""Regression tests for the OpenAI token-limit parameter and its self-heal.

Covers the crash seen while dogfooding: a gpt-5.x model rejects ``max_tokens``
with "Unsupported parameter: 'max_tokens' ... Use 'max_completion_tokens'".
"""

import json

import httpx

import goldcomb.providers.openai as oai
from goldcomb.providers.base import Completed, TextDelta
from goldcomb.providers.openai import (
    OpenAICompatibleProvider,
    OpenAIProvider,
    _adjust_body_for_error,
)

# The verbatim error string from the dogfooding session.
GPT5_ERROR = (
    "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead."
)


# ---- token-param selection --------------------------------------------------


def test_real_openai_defaults_to_max_completion_tokens():
    p = OpenAIProvider("openai", {"type": "openai"})
    assert p._token_param("gpt-5.6-luna") == "max_completion_tokens"
    assert p._token_param("gpt-4o") == "max_completion_tokens"


def test_compatible_defaults_to_max_tokens():
    p = OpenAICompatibleProvider("groq", {"type": "openai-compatible", "base_url": "x"})
    assert p._token_param("llama-3.1-70b") == "max_tokens"


def test_token_param_config_override():
    cfg = {"type": "openai-compatible", "base_url": "x", "token_param": "max_completion_tokens"}
    p = OpenAICompatibleProvider("ep", cfg)
    assert p._token_param("anything") == "max_completion_tokens"


# ---- self-heal adjustment logic --------------------------------------------


def test_adjust_swaps_max_tokens_to_completion():
    body = {"model": "gpt-5.6-luna", "max_tokens": 4096}
    fixed = _adjust_body_for_error(body, f"HTTP 400: {GPT5_ERROR}")
    assert fixed is not None
    assert "max_tokens" not in fixed
    assert fixed["max_completion_tokens"] == 4096
    assert body["max_tokens"] == 4096  # original untouched


def test_adjust_swaps_completion_back_to_max_tokens():
    body = {"max_completion_tokens": 2048}
    msg = "HTTP 400: Unrecognized request argument supplied: max_tokens is required"
    fixed = _adjust_body_for_error(body, msg)
    assert fixed is not None
    assert fixed["max_tokens"] == 2048
    assert "max_completion_tokens" not in fixed


def test_adjust_drops_unsupported_temperature():
    body = {"max_completion_tokens": 100, "temperature": 0.2}
    msg = "HTTP 400: Unsupported value: 'temperature' does not support 0.2 with this model"
    fixed = _adjust_body_for_error(body, msg)
    assert fixed is not None
    assert "temperature" not in fixed


def test_adjust_returns_none_for_unrelated_error():
    body = {"max_tokens": 4096}
    assert _adjust_body_for_error(body, "HTTP 401: invalid api key") is None


def test_adjust_does_not_reswap_when_already_correct():
    # If we already sent max_completion_tokens and the server still complains
    # about it, there is nothing left to swap -> None (prevents retry loops).
    body = {"max_completion_tokens": 4096}
    assert _adjust_body_for_error(body, f"HTTP 400: {GPT5_ERROR}") is None


# ---- end-to-end: the stream recovers from the 400 --------------------------


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def test_stream_self_heals_and_completes(monkeypatch):
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_bodies.append(payload)
        if "max_tokens" in payload:  # first attempt: reject like gpt-5.x does
            return httpx.Response(400, json={"error": {"message": GPT5_ERROR}})
        return httpx.Response(  # retry with max_completion_tokens: succeed
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            ),
        )

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(oai.httpx, "Client", fake_client)

    p = OpenAICompatibleProvider(
        "gw", {"type": "openai-compatible", "base_url": "http://mock", "api_key": "k"}
    )
    events = list(p.stream([], model="gpt-5.6-luna", max_tokens=4096))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    completed = [e for e in events if isinstance(e, Completed)]
    assert text == "Hello"
    assert completed and completed[0].usage["output_tokens"] == 1
    # It tried max_tokens first, then self-healed to max_completion_tokens.
    assert "max_tokens" in seen_bodies[0]
    assert "max_completion_tokens" in seen_bodies[1]


REASONING_TOOLS_ERROR = (
    "HTTP 400: Function tools with reasoning_effort are not supported for "
    "gpt-5.6-sol in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'."
)


def test_reasoning_effort_tools_rejection_self_heals():
    body = {"model": "gpt-5.6-sol", "messages": [], "tools": [{"type": "function"}]}
    fixed = _adjust_body_for_error(body, REASONING_TOOLS_ERROR)
    assert fixed is not None
    assert fixed["reasoning_effort"] == "none"
    # already pinned to none -> nothing left to fix; surface the error
    assert _adjust_body_for_error(fixed, REASONING_TOOLS_ERROR) is None
