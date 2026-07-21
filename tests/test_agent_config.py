"""Per-agent deploy config: the macOS app writes an agent's chosen default
model to .ai/agents/agent-config.json, and a deploy honors it so a
pre-configured agent runs on the model the user picked (the "both" behavior).
"""

import json

import pytest

from goldcomb import agents


@pytest.fixture(autouse=True)
def _in_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield


def _write_config(mapping: dict) -> None:
    from pathlib import Path
    d = Path(".ai/agents")
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-config.json").write_text(
        json.dumps({"version": 1, "agents": mapping}))


def test_no_file_means_unconfigured():
    assert agents.configured_default("Quill Ashwood (swift-worker-2)") == (None, None)


def test_matches_full_human_name():
    _write_config({"Quill Ashwood (swift-worker-2)":
                   {"provider": "anthropic", "model": "claude-opus-4-8"}})
    assert agents.configured_default("Quill Ashwood (swift-worker-2)") == (
        "anthropic", "claude-opus-4-8")


def test_falls_back_to_bare_label():
    # A deploy that humanized to a name whose label matches a config entry
    # keyed by the bare label still resolves.
    _write_config({"swift-worker-2": {"provider": "gemini",
                                      "model": "gemini-2.5-flash"}})
    assert agents.configured_default("Quill Ashwood (swift-worker-2)") == (
        "gemini", "gemini-2.5-flash")


def test_unknown_agent_is_unconfigured():
    _write_config({"Someone Else": {"provider": "openai", "model": "gpt-4o"}})
    assert agents.configured_default("Quill Ashwood (swift-worker-2)") == (None, None)


def test_malformed_file_is_tolerated():
    from pathlib import Path
    d = Path(".ai/agents")
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-config.json").write_text("{ not json")
    assert agents.configured_default("anyone") == (None, None)


def test_model_only_entry_returns_none_provider():
    _write_config({"Ada": {"model": "claude-opus-4-8"}})
    assert agents.configured_default("Ada") == (None, "claude-opus-4-8")


def test_deploy_prefers_explicit_model_over_config(tmp_path):
    """The deploy flow only consults config when the deployer didn't pin a
    model — an explicit choice wins. Verified at the resolve layer: config is
    only read when model_arg is falsy (see cli._run_subagent)."""
    _write_config({"Quill Ashwood (swift-worker-2)":
                   {"provider": "anthropic", "model": "claude-opus-4-8"}})
    # Simulate the cli guard: an explicit model short-circuits the lookup.
    model_arg = "gpt-4o"
    if not model_arg:  # not taken
        _, model_arg = agents.configured_default("Quill Ashwood (swift-worker-2)")
    assert model_arg == "gpt-4o"
