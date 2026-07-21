"""Tests for the claude engine (goldcomb/engines/claude.py).

Hermetic: the Claude Agent SDK is never imported. A fake ``query_fn`` (an async
generator of fakes named like the SDK's message/block classes) drives the async
bridge, and ``sys.modules["claude_agent_sdk"] = None`` forces the not-installed
path on demand.
"""

import sys

import pytest

from goldcomb.engines import ENGINES
from goldcomb.engines.claude import (
    SDK_MISSING_MSG,
    ClaudeEngine,
    build_prompt,
    map_message,
    run_async_stream,
    sdk_available,
    _fmt_tool,
    _usage_dict,
)
from goldcomb.providers.base import (
    Completed,
    Message,
    ProviderError,
    TextDelta,
    ThinkingDelta,
)


# -- fakes shaped/named like the SDK's types (matched by class name) ----------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TextBlock(_Obj):
    pass


class ThinkingBlock(_Obj):
    pass


class ToolUseBlock(_Obj):
    pass


class AssistantMessage(_Obj):
    pass


class ResultMessage(_Obj):
    pass


def fake_query(messages, captured=None):
    """Return an async-generator function shaped like ``claude_agent_sdk.query``."""
    async def query(prompt, options):
        if captured is not None:
            captured["prompt"] = prompt
            captured["options"] = options
        for m in messages:
            yield m
    return query


# -- build_prompt -------------------------------------------------------------

def test_build_prompt_empty_and_single():
    assert build_prompt([]) == ""
    assert build_prompt([Message(role="user", content="do the thing")]) == "do the thing"


def test_build_prompt_folds_prior_context():
    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="ok"),
        Message(role="user", content="second"),
    ]
    prompt = build_prompt(msgs)
    assert "[Earlier conversation for context]" in prompt
    assert "User: first" in prompt
    assert "Assistant: ok" in prompt
    assert prompt.rstrip().endswith("second")
    # tool-role turns and empty content are dropped from the context block
    assert "tool" not in prompt.lower().split("[current request]")[0]


def test_build_prompt_respects_context_turns_cap():
    msgs = [Message(role="user", content=f"m{i}") for i in range(10)]
    msgs.append(Message(role="user", content="latest"))
    prompt = build_prompt(msgs, context_turns=2)
    # only the last two prior turns survive
    assert "m9" in prompt and "m8" in prompt
    assert "m0" not in prompt


# -- mapping helpers ----------------------------------------------------------

def test_map_message_dispatches_blocks():
    msg = AssistantMessage(content=[
        TextBlock(text="hi "),
        ToolUseBlock(name="Read", input={"file_path": "a.py"}),
        ThinkingBlock(thinking="pondering"),
        TextBlock(text=""),  # empty text is skipped
    ])
    items = list(map_message(msg))
    assert ("text", "hi ") in items
    assert ("thinking", "pondering") in items
    kinds = [k for k, _ in items]
    assert "activity" in kinds
    assert kinds.count("text") == 1  # empty TextBlock dropped
    activity = next(v for k, v in items if k == "activity")
    assert "Read" in activity and "a.py" in activity


def test_map_message_usage():
    msg = ResultMessage(usage={"input_tokens": 7, "output_tokens": 2})
    assert list(map_message(msg)) == [("usage", {"input_tokens": 7, "output_tokens": 2})]


def test_usage_dict_from_object_and_dict():
    class U:
        input_tokens = 5
        output_tokens = 1
        cache_read_input_tokens = 3
        cache_creation_input_tokens = None  # non-int is dropped
    assert _usage_dict(U()) == {
        "input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 3}
    assert _usage_dict({"input_tokens": 9}) == {"input_tokens": 9}
    assert _usage_dict(None) == {}


def test_fmt_tool_picks_a_detail_field():
    assert "b.py" in _fmt_tool("Edit", {"file_path": "b.py"})
    assert "ls -la" in _fmt_tool("Bash", {"command": "ls -la"})
    assert "Glob" in _fmt_tool("Glob", {})  # no detail field is fine


# -- run_async_stream ---------------------------------------------------------

def test_run_async_stream_yields_in_order():
    async def agen():
        for m in (AssistantMessage(content=[TextBlock(text="a")]),
                  AssistantMessage(content=[TextBlock(text="b")])):
            yield m
    out = list(run_async_stream(agen, map_message))
    assert out == [("text", "a"), ("text", "b")]


def test_run_async_stream_wraps_sdk_error():
    class CLINotFoundError(Exception):
        pass

    async def agen():
        raise CLINotFoundError("missing")
        yield  # pragma: no cover - marks this an async generator

    with pytest.raises(ProviderError) as ei:
        list(run_async_stream(agen, map_message))
    assert "Claude Code CLI" in str(ei.value)


def test_run_async_stream_passes_through_provider_error():
    async def agen():
        raise ProviderError("already friendly")
        yield  # pragma: no cover

    with pytest.raises(ProviderError) as ei:
        list(run_async_stream(agen, map_message))
    assert str(ei.value) == "already friendly"


def test_run_async_stream_yields_before_raising():
    async def agen():
        yield AssistantMessage(content=[TextBlock(text="partial")])
        raise RuntimeError("boom")

    gen = run_async_stream(agen, map_message)
    assert next(gen) == ("text", "partial")
    with pytest.raises(ProviderError):
        next(gen)


# -- ClaudeEngine.stream (end to end, no SDK) ---------------------------------

def test_stream_emits_deltas_activity_and_completed():
    captured = {}
    messages = [
        AssistantMessage(content=[
            TextBlock(text="Hello "),
            ToolUseBlock(name="Read", input={"file_path": "a.py"}),
            TextBlock(text="world"),
        ]),
        AssistantMessage(content=[ThinkingBlock(thinking="hmm")]),
        ResultMessage(usage={"input_tokens": 10, "output_tokens": 3,
                             "cache_read_input_tokens": 2}),
    ]
    eng = ClaudeEngine("claude", {"type": "anthropic", "api_key": "K"},
                       query_fn=fake_query(messages, captured))
    events = list(eng.stream([Message(role="user", content="hi")],
                             model="claude-opus-4-8", system="SYS"))

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts[0] == "Hello "
    assert "Read" in texts[1]  # the tool-activity note
    assert texts[2] == "world"
    assert any(isinstance(e, ThinkingDelta) and e.text == "hmm" for e in events)

    completed = events[-1]
    assert isinstance(completed, Completed)
    # activity text is shown but NOT folded into the saved assistant message
    assert completed.message.content == "Hello world"
    assert completed.message.role == "assistant"
    assert completed.stop_reason == "end_turn"
    assert completed.usage == {"input_tokens": 10, "output_tokens": 3,
                               "cache_read_input_tokens": 2}
    # the prompt reached the SDK
    assert captured["prompt"] == "hi"


def test_stream_missing_sdk_raises_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # force ImportError
    eng = ClaudeEngine("claude", {"type": "anthropic"})  # no query_fn -> resolve SDK
    with pytest.raises(ProviderError) as ei:
        list(eng.stream([Message(role="user", content="hi")], model="m"))
    assert "claude-agent-sdk" in str(ei.value)
    assert SDK_MISSING_MSG.splitlines()[0] in str(ei.value)


# -- options / permission mode ------------------------------------------------

def test_build_options_dict_fallback_and_env(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # force dict fallback
    eng = ClaudeEngine("claude", {"type": "anthropic", "api_key": "K",
                                  "base_url": "https://x"}, auto_approve=False)
    opts = eng._build_options(model="claude-opus-4-8", system="SYS")
    assert isinstance(opts, dict)
    assert opts["model"] == "claude-opus-4-8"
    assert opts["system_prompt"] == "SYS"
    assert opts["permission_mode"] == "acceptEdits"
    assert opts["max_turns"] == 40
    assert opts["cwd"]
    assert opts["env"]["ANTHROPIC_API_KEY"] == "K"
    assert opts["env"]["ANTHROPIC_BASE_URL"] == "https://x"
    # inherits the ambient environment rather than replacing it
    assert "PATH" in opts["env"]


def test_permission_mode_variants():
    assert ClaudeEngine("c", {})._permission_mode() == "acceptEdits"
    assert ClaudeEngine("c", {}, auto_approve=True)._permission_mode() == "bypassPermissions"
    assert ClaudeEngine("c", {"claude_permission_mode": "plan"})._permission_mode() == "plan"


def test_no_api_key_means_no_env_override(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    opts = ClaudeEngine("c", {"type": "anthropic"})._build_options(model="m", system=None)
    assert "env" not in opts
    assert "system_prompt" not in opts


# -- misc ---------------------------------------------------------------------

def test_engines_constant_and_sdk_probe():
    assert ENGINES == ("native", "claude")
    assert isinstance(sdk_available(), bool)  # never raises
