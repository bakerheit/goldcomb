"""/compact — summarize the conversation and continue from the summary.

Compaction is the same context-cost lever the group-chat work targeted, but
user-driven: it replaces a long history with one dense summary message so the
next turn carries the gist at a fraction of the tokens. These tests pin the
replacement behavior and the guard rails (too-short, empty summary), plus the
serve-mode command wiring.
"""

from goldcomb.providers.base import (
    Completed,
    Event,
    Message,
    Provider,
    TextDelta,
)


class _StubProvider(Provider):
    """A provider whose stream yields a fixed summary — no network."""

    type_name = "stub"

    def __init__(self, summary: str = "SUMMARY: goal X, decided Y, next Z"):
        super().__init__("stub", {"type": "stub"})
        self.summary = summary
        self.calls: list[dict] = []

    def stream(self, messages, *, model, system=None, tools=None,
               max_tokens=4096, temperature=None):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        yield TextDelta(self.summary)
        yield Completed(
            message=Message(role="assistant", content=self.summary),
            stop_reason="end_turn",
            usage={"input_tokens": 100, "output_tokens": 10},
        )

    def list_models(self):
        return ["stub-1"]


def _app(messages):
    """A minimal App wired enough to compact, without a real provider/config."""
    from goldcomb.cli import App
    from goldcomb.config import Config

    app = App.__new__(App)
    app.messages = list(messages)
    app.cfg = Config.load()
    app.cfg.current = {"provider": "stub", "model": "stub-1"}
    app.session_tokens = {"in": 0, "out": 0}
    app.persist = False
    app.thread = None

    # Renderer is only touched for status; a no-op stand-in is enough.
    class _R:
        def start_status(self, label): pass
        def stop_status(self): pass
        def usage(self, *_a, **_k): pass
    app.renderer = _R()
    return app


def _convo(n_pairs: int) -> list[Message]:
    out: list[Message] = []
    for i in range(n_pairs):
        out.append(Message(role="user", content=f"question {i}"))
        out.append(Message(role="assistant", content=f"answer {i}"))
    return out


# -- core behavior -----------------------------------------------------------

def test_compaction_replaces_history_with_one_summary_message():
    app = _app(_convo(4))  # 8 messages
    result = app.compact_conversation(_StubProvider())
    assert result["ok"] is True
    assert result["before"] == 8 and result["after"] == 1
    assert len(app.messages) == 1
    only = app.messages[0]
    assert only.role == "user"
    assert only.content.startswith("[Earlier conversation, compacted")
    assert "SUMMARY: goal X" in only.content


def test_summary_call_uses_no_tools():
    """Compaction is a plain summarization — tools would only add noise/cost."""
    app = _app(_convo(4))
    stub = _StubProvider()
    app.compact_conversation(stub)
    assert stub.calls[0]["tools"] is None
    assert "compacting" in stub.calls[0]["system"].lower()


def test_transcript_includes_tool_activity():
    from goldcomb.providers.base import ToolCall
    msgs = [
        Message(role="user", content="read the file"),
        Message(role="assistant", content="",
                tool_calls=[ToolCall(id="t1", name="read_file",
                                     arguments={"path": "x.py"})]),
        Message(role="tool", content="file contents here", tool_call_id="t1"),
        Message(role="assistant", content="done"),
    ]
    app = _app(msgs)
    text = app._render_transcript(msgs)
    assert "read_file" in text and "tool result" in text


def test_usage_from_the_summary_call_is_recorded():
    app = _app(_convo(4))
    app.compact_conversation(_StubProvider())
    assert app.session_tokens["in"] == 100
    assert app.session_tokens["out"] == 10


# -- guard rails -------------------------------------------------------------

def test_short_conversation_is_left_untouched():
    app = _app(_convo(1))  # 2 messages — not worth summarizing
    result = app.compact_conversation(_StubProvider())
    assert result["ok"] is False and result["reason"] == "too-short"
    assert len(app.messages) == 2  # unchanged


def test_empty_summary_leaves_history_intact():
    app = _app(_convo(4))
    result = app.compact_conversation(_StubProvider(summary="   "))
    assert result["ok"] is False and result["reason"] == "empty-summary"
    assert len(app.messages) == 8  # not replaced with an empty summary


# -- serve wiring ------------------------------------------------------------

def test_serve_compact_command_emits_compacted_event(monkeypatch):
    from goldcomb import server

    app = _app(_convo(4))
    app.get_provider = lambda: _StubProvider()  # type: ignore[method-assign]
    app._autosave = lambda: None                # type: ignore[method-assign]

    events: list[dict] = []
    server._dispatch(app, app.cfg, {"type": "compact"}, events.append)

    assert events and events[-1]["event"] == "compacted"
    assert events[-1]["ok"] is True
    assert events[-1]["before"] == 8 and events[-1]["after"] == 1
    assert len(app.messages) == 1
