"""Tests for the --serve NDJSON protocol layer."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from goldcomb.config import Config
from goldcomb.server import JsonEventRenderer, _dispatch


class Sink:
    def __init__(self):
        self.events = []

    def __call__(self, obj):
        self.events.append(obj)

    def kinds(self):
        return [e["event"] for e in self.events]


# ---- JsonEventRenderer ------------------------------------------------------


def test_renderer_emits_full_turn_sequence():
    sink = Sink()
    r = JsonEventRenderer(sink)
    r.start_status("Thinking")
    r.tool_call("$ ls")
    r.tool_result("a\nb")
    r.begin_message("kimi", "kimi-k3")
    r.message_delta("hel")
    r.message_delta("lo")
    r.end_message()
    r.usage({"input_tokens": 5, "output_tokens": 2}, {"in": 5, "out": 2})
    assert sink.kinds() == [
        "status", "tool_call", "tool_result",
        "status",  # begin_message clears the spinner (label: null)
        "message_start", "delta", "delta", "message_end", "usage",
    ]
    end = sink.events[-2]
    assert end == {"event": "message_end", "text": "hello"}


def test_renderer_dedupes_status_and_clears_once():
    sink = Sink()
    r = JsonEventRenderer(sink)
    r.start_status("Thinking")
    r.update_status("Thinking")   # duplicate — no event
    r.update_status("Running x")
    r.stop_status()
    r.stop_status()               # already clear — no event
    labels = [e["label"] for e in sink.events]
    assert labels == ["Thinking", "Running x", None]


def test_renderer_has_the_full_renderer_surface():
    # App drives these; a missing one would crash a turn at runtime.
    r = JsonEventRenderer(lambda e: None)
    r.install_resize_handler()
    r.on_resize()
    r.nudge("careful")
    r.stop_all()
    r.footer = lambda: ("a", "b")  # assignable, like ui.Renderer


# ---- _dispatch --------------------------------------------------------------


class DummyApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.auto_approve = False
        self.session_tokens = {"in": 0, "out": 0}
        self.turns = []

    def run_turn(self, text):
        self.turns.append(text)


def make_cfg(tmp_path):
    path = tmp_path / "config.json"
    data = {
        "providers": {
            "kimi": {"type": "openai-compatible", "api_key": "k"},
            "openai": {"type": "openai", "api_key": "k"},
        },
        "current": {"provider": "kimi", "model": "kimi-k3"},
        "settings": {},
    }
    path.write_text(json.dumps(data))
    return Config(data, path)


def test_dispatch_user_runs_turn_and_ends(tmp_path):
    cfg = make_cfg(tmp_path)
    app, sink = DummyApp(cfg), Sink()
    _dispatch(app, cfg, {"type": "user", "text": "hi"}, sink)
    assert app.turns == ["hi"]
    # The thread id is announced at turn START (the GUI's "chat id" must not
    # wait out a long turn), then the turn ends.
    assert sink.kinds() == ["thread", "turn_end"]
    assert sink.events[0]["thread_id"]
    assert sink.events[-1]["thread_id"] == sink.events[0]["thread_id"]


def test_dispatch_use_switches_in_memory_only(tmp_path):
    cfg = make_cfg(tmp_path)
    before = cfg.path.read_text()
    app, sink = DummyApp(cfg), Sink()
    _dispatch(app, cfg, {"type": "use", "provider": "openai"}, sink)
    assert cfg.current_provider == "openai"
    assert cfg.current_model  # fell back to the type default
    assert sink.events[0]["event"] == "using"
    # The config file must be untouched: parallel sessions share it.
    assert cfg.path.read_text() == before


def test_dispatch_rejects_unknown_provider_and_command(tmp_path):
    cfg = make_cfg(tmp_path)
    app, sink = DummyApp(cfg), Sink()
    _dispatch(app, cfg, {"type": "use", "provider": "nope"}, sink)
    _dispatch(app, cfg, {"type": "frobnicate"}, sink)
    assert sink.kinds() == ["error", "error"]


def test_dispatch_sudo_toggles(tmp_path):
    cfg = make_cfg(tmp_path)
    app, sink = DummyApp(cfg), Sink()
    _dispatch(app, cfg, {"type": "sudo", "on": True}, sink)
    assert app.auto_approve is True
    _dispatch(app, cfg, {"type": "sudo", "on": False}, sink)
    assert app.auto_approve is False


# ---- scrum_action (GUI board edits) -----------------------------------------


def test_dispatch_scrum_action_runs_board_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the board lives under the cwd, like --serve
    cfg = make_cfg(tmp_path)
    app, sink = DummyApp(cfg), Sink()

    _dispatch(app, cfg, {"type": "scrum_action", "action": "init", "project": "demo"}, sink)
    ev = sink.events[0]
    assert ev["event"] == "scrum_result"
    assert ev["action"] == "init"
    assert ev["ok"] is True

    _dispatch(app, cfg, {"type": "scrum_action", "action": "story_add", "title": "GUI story"}, sink)
    sid = re.search(r"\b[A-Z][A-Z0-9]{0,5}-\d+\b", sink.events[-1]["message"]).group(0)
    _dispatch(app, cfg, {
        "type": "scrum_action", "action": "task_add",
        "story": sid, "title": "GUI task", "points": 2,
    }, sink)
    ev = sink.events[-1]
    assert ev["ok"] is True
    tid = re.search(r"\b[A-Z][A-Z0-9]{0,5}-\d+\b", ev["message"]).group(0)

    # The card move a kanban column-drop performs:
    _dispatch(app, cfg, {
        "type": "scrum_action", "action": "task_update",
        "task": tid, "status": "in_progress",
    }, sink)
    assert sink.events[-1]["ok"] is True

    import goldcomb.scrum as scrum
    board = scrum.load_board()
    task = board["stories"][sid]["tasks"][0]
    assert task["id"] == tid
    assert task["status"] == "in_progress"
    assert task["points"] == 2


def test_dispatch_scrum_action_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # scrum.py resolves the board relative to its cwd at import time; make
    # sure this test's tmp dir is the one it sees (another test's chdir can
    # leak via the shared module otherwise).
    import importlib

    import goldcomb.scrum as scrum
    importlib.reload(scrum)

    cfg = make_cfg(tmp_path)
    app, sink = DummyApp(cfg), Sink()

    _dispatch(app, cfg, {"type": "scrum_action"}, sink)
    assert sink.kinds() == ["error"]

    # Unknown actions name the valid ones.
    _dispatch(app, cfg, {"type": "scrum_action", "action": "bogus"}, sink)
    ev = sink.events[-1]
    assert ev["event"] == "scrum_result"
    assert ev["ok"] is False
    assert "unknown action" in ev["message"]


# ---- subprocess smoke (no network) -----------------------------------------


def test_serve_subprocess_handshake_and_exit(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "providers": {}, "current": {}, "settings": {}, "models_cache": {},
    }))
    proc = subprocess.run(
        [sys.executable, "-m", "goldcomb", "--serve"],
        input='{"type":"user","text":"hi"}\n{"type":"exit"}\n',
        capture_output=True, text=True, timeout=30,
        env={
            "GOLDCOMB_CONFIG_DIR": str(cfg_dir),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "ready"
    # No provider configured: the turn still starts and ends cleanly, with the
    # human-readable complaint going to stderr, not stdout.
    assert "turn_end" in kinds
    assert "No provider configured" in proc.stderr


# ---- thread history over the protocol ----------------------------------------


@pytest.fixture()
def serve_env(tmp_path, monkeypatch):
    """Isolated config/threads dir, with everything that reads it reloaded.
    chdir too: threads are cwd-scoped, and the repo root has real history."""
    monkeypatch.setenv("GOLDCOMB_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    import importlib

    import goldcomb.config as config
    import goldcomb.threads as threads
    import goldcomb.server as server
    importlib.reload(config)
    importlib.reload(threads)
    importlib.reload(server)
    return tmp_path, threads, server


def test_dispatch_threads_lists_saved(serve_env):
    _, threads, server = serve_env
    t = threads.new_thread(provider="openai", model="gpt-4o")
    t.messages = [{"role": "user", "content": "hello from the app"}]
    threads.save_thread(t)

    cfg = Config.load()
    sink = Sink()
    server._dispatch(DummyApp(cfg), cfg, {"type": "threads"}, sink)
    ev = sink.events[0]
    assert ev["event"] == "threads"
    assert len(ev["threads"]) == 1
    row = ev["threads"][0]
    assert row["id"] == t.id
    assert row["title"].startswith("hello from the app")
    assert row["message_count"] == 1
    assert "messages" not in row  # summaries stay light


def test_dispatch_resume_adopts_thread(serve_env, monkeypatch, tmp_path):
    _, threads, server = serve_env
    monkeypatch.chdir(tmp_path)  # resume resolves against cwd
    t = threads.new_thread(provider="openai", model="gpt-4o")
    t.messages = [{"role": "user", "content": "remember blue"}]
    threads.save_thread(t)

    from goldcomb.cli import App
    from rich.console import Console

    cfg = Config.load()
    app = App(cfg, Console(record=True, width=100))
    sink = Sink()
    server._dispatch(app, cfg, {"type": "resume", "id": t.id[:12]}, sink)
    ev = sink.events[0]
    assert ev["event"] == "resumed"
    assert ev["thread_id"] == t.id
    assert app.thread.id == t.id
    assert app.messages[0].content == "remember blue"

    # Unknown ids are errors, not crashes.
    sink = Sink()
    server._dispatch(app, cfg, {"type": "resume", "id": "nope-nope"}, sink)
    assert sink.events[0]["event"] == "error"
    sink = Sink()
    server._dispatch(app, cfg, {"type": "resume"}, sink)
    assert sink.events[0]["event"] == "error"


def test_serve_handshake_reports_cwd(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "providers": {}, "current": {}, "settings": {}, "models_cache": {},
    }))
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "goldcomb", "--serve"],
        input='{"type":"exit"}\n',
        capture_output=True, text=True, timeout=30,
        env={
            "GOLDCOMB_CONFIG_DIR": str(cfg_dir),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        cwd=work,
    )
    assert proc.returncode == 0
    ready = json.loads(proc.stdout.splitlines()[0])
    assert ready["event"] == "ready"
    assert Path(ready["cwd"]).resolve() == work.resolve()


# ---- sub-agent lifecycle events (NEXA-3) -------------------------------------


def test_renderer_subagent_lifecycle_events():
    sink = Sink()
    r = JsonEventRenderer(sink)
    r.subagent_start(
        id="a1", label="worker-1", task="do the thing",
        parent=None, provider="kimi", model="kimi-k3",
    )
    r.subagent_end(
        id="a1", label="worker-1", stop_reason="completed", iterations=2,
        tool_calls=1, usage={"in": 10, "out": 5},
        transcript_path=".ai/threads/x.jsonl", error=None,
    )
    assert sink.events == [
        {
            "event": "subagent_start", "id": "a1", "label": "worker-1",
            "task": "do the thing", "parent": None,
            "provider": "kimi", "model": "kimi-k3",
        },
        {
            "event": "subagent_end", "id": "a1", "label": "worker-1",
            "stop_reason": "completed", "iterations": 2, "tool_calls": 1,
            "usage": {"in": 10, "out": 5},
            "transcript_path": ".ai/threads/x.jsonl", "error": None,
        },
    ]


class _FakeSubProvider:
    """One scripted event list per stream() call (same pattern as test_agents)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)

    def stream(self, messages, *, model, system=None, tools=None,
               max_tokens=4096, temperature=None):
        yield from self.scripts.pop(0)


def _subagent_app(serve_env, fake):
    """An App whose renderer is a JsonEventRenderer and whose sub-agent
    provider build is stubbed to the given fake."""
    from rich.console import Console

    from goldcomb import threads as threads_mod
    from goldcomb.cli import App
    from goldcomb.providers import Completed, Message

    tmp_path, _, server = serve_env
    cfg = Config(
        {
            "providers": {"kimi": {"type": "openai-compatible", "api_key": "k"}},
            "current": {"provider": "kimi", "model": "kimi-k3"},
            "settings": {},
        },
        tmp_path / "cfg" / "config.json",
    )
    app = App(cfg, Console(record=True, width=100))
    sink = Sink()
    app.renderer = server.JsonEventRenderer(sink)
    # A live thread so subagent_start can name its parent session.
    t = threads_mod.new_thread(provider="kimi", model="kimi-k3")
    t.messages = [{"role": "user", "content": "hi"}]
    threads_mod.save_thread(t)
    app.thread = t
    app._test_fake = fake
    import goldcomb.cli as cli_mod

    def _fake_build(name, entry):
        return fake

    orig = cli_mod.build_provider
    cli_mod.build_provider = _fake_build
    return app, sink, Completed, Message, lambda: setattr(cli_mod, "build_provider", orig)


def test_run_subagent_emits_bracketing_events(serve_env):
    fake = _FakeSubProvider([])
    app, sink, Completed, Message, restore = _subagent_app(serve_env, fake)
    try:
        fake.scripts.append([
            Completed(
                message=Message(role="assistant", content="report: done"),
                stop_reason="end_turn",
                usage={"input_tokens": 3, "output_tokens": 2},
            )
        ])
        out = app._run_subagent({"label": "worker-1", "task": "do the thing"})
    finally:
        restore()
    assert "report: done" in out
    lifecycle = [e for e in sink.events if e["event"].startswith("subagent_")]
    assert [e["event"] for e in lifecycle] == ["subagent_start", "subagent_end"]
    start, end = lifecycle
    # Deploy labels are humanized ("Ada Gable (worker-1)") so agents read as
    # people; the functional label survives parenthetically.
    assert start["label"].endswith("(worker-1)")
    from goldcomb.names import looks_human
    assert looks_human(start["label"].split(" (")[0])
    assert start["task"] == "do the thing"
    assert start["parent"] == app.thread.id
    assert start["provider"] == "kimi" and start["model"] == "kimi-k3"
    assert end["id"] == start["id"]
    assert end["stop_reason"] == "completed"
    assert end["iterations"] == 1
    assert end["tool_calls"] == 0
    assert end["usage"] == {"in": 3, "out": 2}
    assert end["error"] is None
    assert end["transcript_path"]  # every run leaves an inspectable record


def test_run_subagent_emits_error_end_on_provider_failure(serve_env):
    from goldcomb.providers import ProviderError

    class ExplodingProvider:
        def stream(self, messages, **kwargs):
            raise ProviderError("boom")
            yield  # pragma: no cover - marks this a generator

    app, sink, *_, restore = _subagent_app(serve_env, ExplodingProvider())
    try:
        out = app._run_subagent({"label": "worker-2", "task": "explode"})
    finally:
        restore()
    assert out.startswith("Error: sub-agent failed: boom")
    lifecycle = [e for e in sink.events if e["event"].startswith("subagent_")]
    assert [e["event"] for e in lifecycle] == ["subagent_start", "subagent_end"]
    end = lifecycle[1]
    assert end["id"] == lifecycle[0]["id"]
    assert end["label"].endswith("(worker-2)")
    assert end["stop_reason"] == "error"
    assert end["error"] == "boom"
    assert end["iterations"] == 0 and end["tool_calls"] == 0
    assert end["usage"] == {"in": 0, "out": 0}
    assert end["transcript_path"] is None
