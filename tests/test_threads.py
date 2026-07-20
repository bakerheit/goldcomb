"""Tests for per-project conversation threads and thread commands."""

import importlib
import json
import os

import pytest

from rich.console import Console


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    """Point config + threads at a throwaway dir and reload the modules that
    read the env var at import time. Also chdir into the tmp dir: threads are
    project-scoped by cwd (including .ai/threads adoption), so running from
    the repo root would pick up the repo's own real thread history."""
    monkeypatch.setenv("GOLDCOMB_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    import goldcomb.config as config
    import goldcomb.threads as threads
    importlib.reload(config)
    importlib.reload(threads)
    return tmp_path


@pytest.fixture()
def app(cfg_dir):
    from goldcomb.config import Config
    import goldcomb.cli as cli
    importlib.reload(cli)
    cfg = Config.load()
    cfg.add_provider("openai", "openai", api_key="sk-test")
    return cli.App(cfg, Console(record=True, width=100))


def _threads():
    import goldcomb.threads as threads
    return threads


def test_new_save_list_load(cfg_dir):
    threads = _threads()
    assert threads.list_threads() == []
    t = threads.new_thread(provider="openai", model="gpt-4o")
    t.messages = [{"role": "user", "content": "Refactor the config loader"},
                  {"role": "assistant", "content": "ok"}]
    threads.save_thread(t)
    lst = threads.list_threads()
    assert len(lst) == 1
    assert lst[0].title.startswith("Refactor the config loader")
    assert lst[0].message_count == 2
    assert threads.load_thread(t.id).id == t.id
    assert threads.latest_thread().id == t.id


def test_unique_prefix_resolves_but_ambiguous_does_not(cfg_dir):
    threads = _threads()
    a = threads.new_thread()
    a.messages = [{"role": "user", "content": "a"}]
    b = threads.new_thread()
    b.messages = [{"role": "user", "content": "b"}]
    threads.save_thread(a)
    threads.save_thread(b)
    # Full ids always resolve.
    assert threads.load_thread(a.id).id == a.id
    # A prefix shared by both (same date) must not resolve to either.
    shared = os.path.commonprefix([a.id, b.id])
    assert threads.load_thread(shared) is None


def test_ordering_is_stable_within_same_second(cfg_dir):
    threads = _threads()
    ids = []
    for i in range(5):
        t = threads.new_thread()
        t.messages = [{"role": "user", "content": str(i)}]
        threads.save_thread(t)
        ids.append(t.id)
    # Most-recently-saved first; microsecond timestamps keep this deterministic.
    listed = [t.id for t in threads.list_threads()]
    assert listed == list(reversed(ids))


def test_delete(cfg_dir):
    threads = _threads()
    t = threads.new_thread()
    t.messages = [{"role": "user", "content": "x"}]
    threads.save_thread(t)
    assert threads.delete_thread(t.id) is True
    assert threads.list_threads() == []
    assert threads.delete_thread("nope") is False


def test_save_exports_generic_interchange_format(cfg_dir, monkeypatch, tmp_path):
    """Each save lands in <cwd>/.ai/threads as JSONL any tool can read;
    the global copy stays canonical and full-fidelity."""
    threads = _threads()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    t = threads.new_thread(provider="openai", model="gpt-4o")
    t.messages = [
        {"role": "user", "content": "persist me in the repo dir"},
        {"role": "assistant", "content": "done",
         "tool_calls": [{"id": "c1", "name": "run_bash", "arguments": {"command": "ls"}}]},
        {"role": "tool", "content": "file.py", "tool_call_id": "c1", "name": "run_bash"},
    ]
    threads.save_thread(t)

    export = proj / ".ai" / "threads" / f"{t.id}.jsonl"
    assert export.is_file()
    lines = [json.loads(line) for line in export.read_text().splitlines()]
    header, msgs = lines[0], lines[1:]
    assert header["type"] == "thread"
    assert header["version"] == 1
    assert header["agent"] == "goldcomb"
    assert header["id"] == t.id and header["provider"] == "openai"
    # Messages are reduced to the portable shape — no provider plumbing.
    assert msgs[0] == {"role": "user", "content": "persist me in the repo dir"}
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_uses"] == ["run_bash"]
    assert "tool_calls" not in msgs[1] and "arguments" not in json.dumps(msgs)
    assert msgs[2] == {"role": "tool", "content": "file.py", "name": "run_bash"}
    # A README describes the format for whatever tool stumbles onto the dir.
    assert (proj / ".ai" / "threads" / "README.md").is_file()
    # The canonical global copy still keeps full fidelity and resumes fine.
    assert threads.load_thread(t.id).messages == t.messages


def test_export_failure_does_not_break_save(cfg_dir, monkeypatch, tmp_path):
    """An unwritable in-project export must not lose the canonical copy."""
    threads = _threads()
    # Point the export at a path whose parent is a file: mkdir raises OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr(threads, "ai_threads_dir",
                        lambda cwd=None: blocker / "threads")
    t = threads.new_thread()
    t.messages = [{"role": "user", "content": "x"}]
    threads.save_thread(t)  # must not raise
    assert threads.load_thread(t.id).messages == t.messages


def _write_foreign_thread(d, thread_id="foreign-1", agent="cursor"):
    """Drop a thread into .ai/threads the way another AI tool would."""
    d.mkdir(parents=True, exist_ok=True)
    header = {"type": "thread", "version": 1, "id": thread_id,
              "title": "chat from another tool", "created": "2026-07-18T10:00:00",
              "updated": "2026-07-18T10:05:00", "agent": agent}
    lines = [json.dumps(header),
             json.dumps({"role": "user", "content": "hello from cursor"}),
             json.dumps({"role": "assistant", "content": "hi!",
                         "timestamp": "2026-07-18T10:01:00"})]
    (d / f"{thread_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_foreign_threads_are_imported(cfg_dir, monkeypatch, tmp_path):
    """A thread written by another tool becomes listable/resumable here."""
    threads = _threads()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    _write_foreign_thread(proj / ".ai" / "threads")

    listed = threads.list_threads()
    assert [t.id for t in listed] == ["foreign-1"]
    t = threads.load_thread("foreign-1")
    assert t.title == "chat from another tool"
    assert t.messages[0] == {"role": "user", "content": "hello from cursor"}
    # Tolerated extras like timestamp don't break resume.
    from goldcomb.providers import Message
    msgs = [Message.from_dict(m) for m in t.messages]
    assert msgs[1].content == "hi!"


def test_import_is_once_and_never_touches_our_files(cfg_dir, monkeypatch, tmp_path):
    threads = _threads()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    foreign_dir = proj / ".ai" / "threads"
    _write_foreign_thread(foreign_dir)
    threads.list_threads()
    canonical = threads.threads_dir() / "foreign-1.json"
    assert canonical.is_file()
    # Mutate the foreign file — no re-import clobbers the canonical copy.
    data = json.loads(canonical.read_text())
    data["title"] = "renamed here"
    canonical.write_text(json.dumps(data))
    assert threads.load_thread("foreign-1").title == "renamed here"

    # Our own exports (agent == goldcomb) are not "foreign".
    mine = threads.new_thread()
    mine.messages = [{"role": "user", "content": "mine"}]
    threads.save_thread(mine)
    assert (foreign_dir / f"{mine.id}.jsonl").is_file()
    assert [t.id for t in threads.list_threads()].count(mine.id) == 1


def test_autosave_creates_and_updates_thread(app):
    from goldcomb.providers import Message
    assert app.thread is None
    app.messages = [Message(role="user", content="hello"),
                    Message(role="assistant", content="hi")]
    app._autosave()
    assert app.thread is not None
    tid = app.thread.id
    threads = _threads()
    assert threads.load_thread(tid).message_count == 2
    # A second turn updates the same thread in place.
    app.messages.append(Message(role="user", content="again"))
    app._autosave()
    assert app.thread.id == tid
    assert threads.load_thread(tid).message_count == 3


def test_persist_flag_disables_autosave(app):
    from goldcomb.providers import Message
    app.persist = False
    app.messages = [Message(role="user", content="ephemeral")]
    app._autosave()
    assert app.thread is None
    assert _threads().list_threads() == []


def test_new_and_clear_detach_thread(app):
    from goldcomb.providers import Message
    app.messages = [Message(role="user", content="hi")]
    app._autosave()
    assert app.thread is not None
    app.cmd_new([])
    assert app.thread is None and app.messages == []
    # /clear also detaches so the old thread is not overwritten empty.
    app.messages = [Message(role="user", content="hi again")]
    app._autosave()
    first = app.thread.id
    app.cmd_clear([])
    assert app.thread is None
    assert _threads().load_thread(first).message_count == 1


def test_resume_by_id_and_number(app):
    from goldcomb.providers import Message
    app.messages = [Message(role="user", content="remember teal")]
    app._autosave()
    tid = app.thread.id
    app.cmd_new([])
    app.cmd_resume([tid])
    assert app.thread.id == tid
    assert app.messages[0].content == "remember teal"
    # number-based resume after listing
    app.cmd_new([])
    app.cmd_threads([])
    app.cmd_resume(["1"])
    assert app.thread.id == tid


def test_resume_bad_id_is_graceful(app):
    app.cmd_resume(["does-not-exist"])  # must not raise
    assert app.thread is None
