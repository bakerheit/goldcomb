"""Empty-message hygiene: a blank assistant turn must never reach a provider.

One empty assistant message in history poisons every later request ("HTTP
400: the message at position N with role 'assistant' must not be empty"), so
it is guarded at every layer: wire builders skip empties, the turn loop
refuses to append them, and thread loading repairs old history.
"""

from goldcomb.providers.anthropic import AnthropicProvider
from goldcomb.providers.base import Message, ToolCall
from goldcomb.threads import Thread, _portable_messages


def _wire(messages):
    provider = AnthropicProvider.__new__(AnthropicProvider)
    return provider._messages_to_wire(messages)


def test_anthropic_wire_drops_empty_turns():
    wire = _wire([
        Message(role="user", content="hi"),
        Message(role="assistant", content=""),          # the poison pill
        Message(role="user", content=""),               # also invalid
        Message(role="assistant", content="hello"),
    ])
    assert [w["role"] for w in wire] == ["user", "assistant"]


def test_anthropic_wire_keeps_tool_only_assistant():
    call = ToolCall(id="t1", name="read_file", arguments={"path": "x"})
    wire = _wire([
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", content="", tool_call_id="t1"),
    ])
    assert wire[0]["role"] == "assistant"
    assert wire[0]["content"][0]["type"] == "tool_use"
    # empty tool output is padded, not dropped — the call still pairs up
    assert wire[1]["content"][0]["content"] == "(no output)"


def test_thread_from_dict_repairs_history():
    thread = Thread.from_dict({
        "id": "t", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ""},               # dropped
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "name": "run_bash", "arguments": {}}]},  # kept
            {"role": "tool", "content": "", "tool_call_id": "1"},    # kept
            {"role": "assistant", "content": "done"},
        ],
    })
    roles = [(m["role"], bool(m.get("content") or m.get("tool_calls")))
             for m in thread.messages]
    assert roles == [("user", True), ("assistant", True),
                     ("tool", False), ("assistant", True)]


def test_portable_export_never_writes_empty_assistant():
    thread = Thread(id="t", cwd="", created="", updated="", messages=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "run_bash", "arguments": {}}]},
        {"role": "assistant", "content": ""},
    ])
    portable = _portable_messages(thread)
    assert len(portable) == 1
    assert portable[0]["content"] == "(called tools: run_bash)"
