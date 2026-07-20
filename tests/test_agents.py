"""Tests for sub-agent deployment: target resolution and the headless loop."""

from pathlib import Path

import pytest

from goldcomb import agents
from goldcomb.config import Config
from goldcomb.providers import Completed, Message, TextDelta, ToolCall
from goldcomb.tools import TOOLS_BY_NAME, describe_call, set_agent_runner


def make_cfg(providers=None, current=None):
    data = {"providers": providers or {}, "current": current or {}, "settings": {}}
    return Config(data, Path("/dev/null"))


class FakeProvider:
    """Yields one scripted event list per stream() call and records requests."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    def stream(self, messages, *, model, system=None, tools=None,
               max_tokens=4096, temperature=None):
        self.requests.append(
            {"messages": list(messages), "model": model, "tools": tools}
        )
        yield from self.scripts.pop(0)


def _completed(content="", tool_calls=(), stop="end_turn", usage=None):
    return Completed(
        message=Message(role="assistant", content=content, tool_calls=list(tool_calls)),
        stop_reason=stop,
        usage=usage or {},
    )


# ---- resolve_target ---------------------------------------------------------


def test_resolve_target_defaults_to_current():
    cfg = make_cfg(
        {"kimi": {"type": "openai-compatible"}},
        {"provider": "kimi", "model": "kimi-k3"},
    )
    assert agents.resolve_target(cfg, None, None) == ("kimi", "kimi-k3")


def test_resolve_target_explicit_model_wins():
    cfg = make_cfg(
        {"kimi": {"type": "openai-compatible"}},
        {"provider": "kimi", "model": "kimi-k3"},
    )
    assert agents.resolve_target(cfg, None, "kimi-k2.6") == ("kimi", "kimi-k2.6")


def test_resolve_target_other_provider_uses_cached_then_type_default():
    cfg = make_cfg(
        {
            "kimi": {"type": "openai-compatible"},
            "openai": {"type": "openai"},
        },
        {"provider": "kimi", "model": "kimi-k3"},
    )
    # No cache → falls back to the provider type's default model.
    name, model = agents.resolve_target(cfg, "openai", None)
    assert name == "openai" and model  # gpt-4o or whatever the type default is
    # Cached live catalog wins over the static type default.
    cfg.models_cache["openai"] = ["o3"]
    assert agents.resolve_target(cfg, "openai", None) == ("openai", "o3")


def test_resolve_target_rejects_unknown_provider():
    cfg = make_cfg({"kimi": {"type": "openai-compatible"}}, {"provider": "kimi"})
    with pytest.raises(ValueError, match="Unknown provider"):
        agents.resolve_target(cfg, "groq", None)


# ---- the headless loop ------------------------------------------------------


def test_subagent_runs_tools_then_reports(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello from disk\n")
    provider = FakeProvider([
        [
            _completed(
                tool_calls=[ToolCall(id="c1", name="read_file",
                                     arguments={"path": str(target)})],
                stop="tool_use",
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        ],
        [
            TextDelta("done"),
            _completed(content="Report: file says hello from disk.",
                       usage={"input_tokens": 20, "output_tokens": 7}),
        ],
    ])
    events = []
    result = agents.run_subagent(
        provider, "kimi-k3", "Read the note and report its contents.",
        on_event=lambda kind, text: events.append((kind, text)),
    )
    assert result.report == "Report: file says hello from disk."
    assert result.iterations == 2 and result.tool_calls == 1
    assert result.usage == {"in": 30, "out": 12}
    # The tool actually ran: its output (file content) went back to the model.
    tool_msgs = [m for m in provider.requests[1]["messages"] if m.role == "tool"]
    assert len(tool_msgs) == 1 and "hello from disk" in tool_msgs[0].content
    assert ("tool", f"read_file({target})") in events


def test_subagent_cannot_deploy_agents():
    names = [t.name for t in agents.subagent_tools()]
    assert "deploy_agent" not in names and "read_file" in names
    # ...but the lead agent's registry does expose it.
    assert "deploy_agent" in TOOLS_BY_NAME


def test_subagent_hits_iteration_ceiling_and_still_reports():
    def tool_use_round():
        return [
            _completed(
                tool_calls=[ToolCall(id="x", name="list_dir", arguments={})],
                stop="tool_use",
            )
        ]

    scripts = [tool_use_round() for _ in range(3)]
    scripts.append([_completed(content="Partial: ran out of steps.")])
    provider = FakeProvider(scripts)
    result = agents.run_subagent(provider, "m", "loop forever", max_iterations=3)
    assert result.report == "Partial: ran out of steps."
    assert result.iterations == 3
    # The wrap-up request must not offer tools.
    assert provider.requests[-1]["tools"] is None


def test_deploy_agent_tool_without_runner_degrades():
    set_agent_runner(None)
    try:
        out = TOOLS_BY_NAME["deploy_agent"].run({"task": "x"})
        assert "not available" in out
    finally:
        set_agent_runner(None)


def test_describe_call_for_deploy_agent():
    s = describe_call(
        "deploy_agent",
        {"task": "scan the repo for TODOs " * 10, "label": "scanner", "model": "o3"},
    )
    assert s.startswith("deploy_agent[scanner → o3]")
    assert len(s) < 110  # long tasks are elided for display
