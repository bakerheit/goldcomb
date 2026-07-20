"""NEXA-16/NEXA-17 (inspectable records) and NEXA-10/NEXA-11 (liveness
registry) close-out tests.

Every sub-agent run — clean finish, step-limit cutoff, or provider error —
must leave an inspectable record: an autosaved transcript in .ai/threads/
(stamped ``goldcomb-subagent:<label>``) and a non-empty report that mentions
where the transcript lives. Live runs also register in agents.REGISTRY with
heartbeat-driven liveness timestamps.
"""

import json

import pytest

from goldcomb import agents
from goldcomb import threads  # noqa: F401  (imported for the module's public surface / re-exports)
from goldcomb.providers import Completed, Message, ToolCall
from goldcomb.providers.base import ProviderError


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


class BoomProvider:
    def stream(self, messages, *, model, system=None, tools=None,
               max_tokens=4096, temperature=None):
        raise ProviderError("HTTP 500: kaboom")


def _completed(content="", tool_calls=(), stop="end_turn", usage=None):
    return Completed(
        message=Message(role="assistant", content=content, tool_calls=list(tool_calls)),
        stop_reason=stop,
        usage=usage or {},
    )


def _tool_use_script():
    return [
        _completed(
            tool_calls=[ToolCall(id="c1", name="list_dir", arguments={})],
            stop="tool_use",
        )
    ]


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Point the canonical store, the .ai export, and the registry's on-disk
    records at tmp_path so tests never touch the real repo's .ai/."""
    monkeypatch.setenv("GOLDCOMB_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "REGISTRY_DIR", tmp_path / ".ai" / "agents")
    return tmp_path


def _transcript_paths(result, tmp_path):
    assert result.transcript_path, "result must say where the transcript lives"
    path = tmp_path / ".ai" / "threads" / f"{result.transcript_path.split('/')[-1]}"
    assert path.exists(), f"transcript file missing: {path}"
    return path


# ---- NEXA-17: every run leaves an inspectable record ------------------------


def test_clean_run_saves_labeled_transcript(isolated_dirs):
    provider = FakeProvider([[_completed(content="all done")]])
    result = agents.run_subagent(provider, "m", "do a thing", label="w-clean")
    assert result.report == "all done"
    assert result.stop_reason == "completed"
    path = _transcript_paths(result, isolated_dirs)
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    # The header says which worker wrote it — that's the inspectability.
    assert header["agent"] == "goldcomb-subagent:w-clean"
    assert header["title"].startswith("do a thing")
    # Full fidelity: the task and the report are both in the record.
    body = "\n".join(lines)
    assert "do a thing" in body and "all done" in body


def test_step_limit_run_still_leaves_record(isolated_dirs):
    # Loops on tool calls until the ceiling, then goes silent at wrap-up.
    scripts = [_tool_use_script() for _ in range(3)]
    scripts.append([_completed(content="")])  # silent wrap-up
    provider = FakeProvider(scripts)
    result = agents.run_subagent(
        provider, "m", "loop forever", max_iterations=3, label="w-limit"
    )
    assert result.stop_reason == "step_limit"
    # Non-empty report even with no final assistant text: the diagnostic
    # footer names the stop reason and the transcript location.
    assert result.report
    assert "step_limit" in result.report
    assert "transcript:" in result.report
    path = _transcript_paths(result, isolated_dirs)
    assert "loop forever" in path.read_text()


def test_provider_error_at_wrapup_still_leaves_record(isolated_dirs):
    class FlakyWrapup(FakeProvider):
        def stream(self, messages, **kw):
            if len(self.requests) >= 1 and self.requests and not self.scripts:
                raise ProviderError("HTTP 500: kaboom")
            return super().stream(messages, **kw)

    scripts = [_tool_use_script()]  # one tool round, then ceiling wrap-up
    provider = FlakyWrapup(scripts)
    # Pop the only script, then the wrap-up call hits the empty-scripts boom.
    orig_stream = provider.stream

    def stream(messages, **kw):
        if provider.scripts:
            return orig_stream(messages, **kw)
        raise ProviderError("HTTP 500: kaboom")

    provider.stream = stream
    result = agents.run_subagent(
        provider, "m", "die at wrap-up", max_iterations=1, label="w-err"
    )
    assert result.stop_reason == "error"
    assert "kaboom" in (result.error or "")
    assert result.report  # diagnostic footer, never empty
    assert "transcript:" in result.report
    _transcript_paths(result, isolated_dirs)


def test_diagnostic_footer_lists_transcript():
    r = agents.SubAgentResult(
        report="", stop_reason="context_exhausted", steps_used=7,
        tool_calls=3, transcript_path=".ai/threads/x.jsonl",
    )
    footer = r.diagnostic_footer()
    assert "context_exhausted" in footer
    assert "7" in footer and "3" in footer
    assert ".ai/threads/x.jsonl" in footer


# ---- NEXA-11: registry with liveness + heartbeats ---------------------------


@pytest.fixture
def clean_registry():
    agents.REGISTRY.clear()
    yield
    agents.REGISTRY.clear()


def test_launch_registers_then_exits_with_terminal_state(
    isolated_dirs, clean_registry
):
    provider = FakeProvider([[_completed(content="reporting in")]])
    handle = agents.launch_subagent(
        provider, "m", "quick task", label="w-live", heartbeat=False
    )
    # Registered while running (or already finished on a fast thread).
    snap = agents.registry_snapshot()
    assert snap["version"] == 1 and "generated_at" in snap
    result = handle.wait(timeout=10)
    assert result is not None and result.report == "reporting in"
    status = handle.status()
    assert status["state"] == "completed"
    assert status["ended_at"] is not None
    assert status["report_saved"] is True
    assert status["label"] == "w-live"
    assert status["transcript_path"]
    # Terminal handles leave the live map but keep an on-disk record.
    assert handle.id not in agents.REGISTRY
    record = isolated_dirs / ".ai" / "agents" / f"{handle.id}.json"
    assert record.exists()
    on_disk = json.loads(record.read_text())
    assert on_disk["state"] == "completed"
    assert on_disk["id"] == handle.id


def test_failed_run_records_error_state(isolated_dirs, clean_registry):
    handle = agents.launch_subagent(
        BoomProvider(), "m", "will explode", label="w-boom", heartbeat=False
    )
    result = handle.wait(timeout=10)
    assert result is not None
    assert result.stop_reason == "error" and "kaboom" in (result.error or "")
    status = handle.status()
    assert status["state"] == "error"
    assert status["error"] and "kaboom" in status["error"]
    record = isolated_dirs / ".ai" / "agents" / f"{handle.id}.json"
    assert json.loads(record.read_text())["state"] == "error"


def test_tool_calls_bump_liveness(isolated_dirs, clean_registry):
    scripts = [_tool_use_script(), [_completed(content="ok")]]
    provider = FakeProvider(scripts)
    handle = agents.launch_subagent(
        provider, "m", "one tool then done", label="w-touch", heartbeat=False
    )
    result = handle.wait(timeout=10)
    assert result is not None and result.tool_calls == 1
    status = handle.status()
    assert status["n_tool_calls"] == 1
    assert status["last_event_at"] >= status["started_at"]


def test_heartbeat_never_kills_a_run(isolated_dirs, clean_registry, monkeypatch):
    def bad_heartbeat(label):
        raise RuntimeError("board is down")

    monkeypatch.setattr(agents.scrum, "heartbeat", bad_heartbeat)
    monkeypatch.setattr(agents, "_HEARTBEAT_INTERVAL_S", 0)  # fire every beat
    provider = FakeProvider([_tool_use_script(), [_completed(content="survived")]])
    result = agents.run_subagent(provider, "m", "beat anyway", label="w-hb")
    assert result.report == "survived"
