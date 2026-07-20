"""Prompt caching: the conversation prefix must be reused, not re-billed.

Every turn re-sends the whole system prompt and history. Uncached, a long
agent session pays full input price for all of it on every single turn — the
dominant cost in agent-to-agent chat, where a three-line message wakes an
agent carrying a large working context. These tests pin the wire shape that
makes the prefix cacheable, and the invariants that keep it that way.
"""

import json

import httpx

from goldcomb.providers import anthropic as anth
from goldcomb.providers.anthropic import AnthropicProvider, _apply_message_caching
from goldcomb.providers.base import Completed, Message, ToolCall


def _provider() -> AnthropicProvider:
    return AnthropicProvider(
        "anthropic",
        {"type": "anthropic", "base_url": "http://mock", "api_key": "k"},
    )


def _wire(messages):
    """The message list as stream() sends it: built, then annotated."""
    wire = AnthropicProvider.__new__(AnthropicProvider)._messages_to_wire(messages)
    _apply_message_caching(wire)
    return wire


def _breakpoints(blocks) -> int:
    return sum(1 for b in blocks if "cache_control" in b)


def _all_blocks(wire):
    return [b for m in wire for b in m["content"]]


# -- wire shape --------------------------------------------------------------

def test_user_turns_are_blocks_not_bare_strings():
    """A bare string can't carry cache_control, so user turns must be blocks."""
    raw = AnthropicProvider.__new__(AnthropicProvider)._messages_to_wire(
        [Message(role="user", content="hi")])
    assert raw[0]["content"] == [{"type": "text", "text": "hi"}]


def test_last_message_carries_a_breakpoint():
    wire = _wire([
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ])
    assert "cache_control" in wire[-1]["content"][-1]


def test_breakpoint_ttl_outlives_an_idle_agent():
    """The 5-minute default would expire between chat wake-ups."""
    wire = _wire([Message(role="user", content="hi")])
    assert wire[0]["content"][-1]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h"}


# -- breakpoint placement ----------------------------------------------------

def test_tool_heavy_turn_gets_a_second_breakpoint():
    """A breakpoint only looks back 20 content blocks for a prior entry, so a
    long agentic turn can strand the previous one. Extra breakpoints keep a
    reachable anchor behind it."""
    messages = [Message(role="user", content="go")]
    for i in range(12):  # one assistant turn per tool call, then its result
        call = ToolCall(id=f"t{i}", name="read_file", arguments={"path": f"{i}"})
        messages.append(Message(role="assistant", content="", tool_calls=[call]))
        messages.append(Message(role="tool", content="ok", tool_call_id=f"t{i}"))
    wire = _wire(messages)
    assert _breakpoints(_all_blocks(wire)) >= 2


def test_never_exceeds_the_api_breakpoint_budget():
    """Four total, and the system block claims one of them."""
    messages = []
    for i in range(60):
        messages.append(Message(role="user", content=f"q{i}"))
        messages.append(Message(role="assistant", content=f"a{i}"))
    wire = _wire(messages)
    assert _breakpoints(_all_blocks(wire)) <= 3


def test_caching_survives_unannotatable_turns():
    """Empty turns are dropped from the wire; placement must not miscount."""
    wire = _wire([
        Message(role="user", content="hi"),
        Message(role="assistant", content=""),
        Message(role="assistant", content="hello"),
    ])
    assert _breakpoints(_all_blocks(wire)) >= 1


def test_placement_is_idempotent():
    wire = _wire([Message(role="user", content="hi")])
    before = _breakpoints(_all_blocks(wire))
    _apply_message_caching(wire)
    assert _breakpoints(_all_blocks(wire)) == before


# -- request body and usage --------------------------------------------------

def _mock_stream(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(anth.httpx, "Client", fake_client)


def _sse(*chunks: dict) -> bytes:
    return "".join(
        f"event: {c['type']}\ndata: {json.dumps(c)}\n\n" for c in chunks
    ).encode()


def test_system_prompt_is_sent_as_a_cached_block(monkeypatch):
    """Blocks render tools -> system -> messages, so one breakpoint on system
    caches the tool definitions with it."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse({"type": "message_stop"}))

    _mock_stream(monkeypatch, handler)
    list(_provider().stream([Message(role="user", content="hi")],
                            model="claude-opus-4-8", system="You are an agent."))

    system = seen[0]["system"]
    assert system[0]["text"] == "You are an agent."
    assert system[0]["cache_control"]["ttl"] == "1h"


def test_system_prompt_is_byte_stable_across_turns(tmp_path, monkeypatch):
    """The invariant the whole scheme rests on.

    The system prompt is the cached prefix for the entire conversation, so a
    single changed byte re-bills every turn behind it at full price. Anything
    that moves between turns — a live chat list, other threads' timestamps —
    belongs in the message stream, not here. If a new dynamic block lands in
    system_prompt(), this fails.
    """
    from rich.console import Console

    from goldcomb import chats as chats_mod
    from goldcomb.cli import App
    from goldcomb.config import Config

    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    cfg.settings["tools_enabled"] = True
    app = App(cfg, Console(record=True, width=100))

    # A room this agent is in — the case the old live-chat block rendered.
    chat_id = chats_mod.start("caching", ["Quill"], text="hello")
    before = app.system_prompt()
    # Exactly the churn a live group chat produces between two turns.
    chats_mod.post(chat_id, "and another thing", author="Quill")

    assert app.system_prompt() == before


def test_cache_usage_is_reported(monkeypatch):
    """Cached reads bill at ~10% and are excluded from input_tokens, so they
    have to surface separately or a working cache looks like a usage drop."""
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "message_start", "message": {"usage": {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 41000,
                    "cache_creation_input_tokens": 300,
                }}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 7}},
            ))

    _mock_stream(monkeypatch, handler)
    events = list(_provider().stream([Message(role="user", content="hi")],
                                     model="claude-opus-4-8"))
    usage = [e for e in events if isinstance(e, Completed)][0].usage
    assert usage["input_tokens"] == 12
    assert usage["cache_read_input_tokens"] == 41000
    assert usage["cache_creation_input_tokens"] == 300
