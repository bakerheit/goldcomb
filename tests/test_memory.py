"""Per-agent memory (goldcomb/memory.py) + recall (goldcomb/recall.py)."""

import json

import pytest

import goldcomb.memory as memory
import goldcomb.recall as recall
import goldcomb.scrum as scrum


@pytest.fixture()
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def as_agent():
    scrum.set_agent("w-tester")
    yield "w-tester"
    scrum.set_agent(None)


# ---- memory -----------------------------------------------------------------

def test_remember_show_roundtrip(cwd, as_agent):
    out = memory.memory_tool({"action": "remember", "text": "prefers pytest -q"})
    assert "Remembered" in out
    path = cwd / ".ai" / "memory" / "w-tester.md"
    assert path.exists()
    assert "# Memory of w-tester" in path.read_text()
    shown = memory.memory_tool({"action": "show"})
    assert "prefers pytest -q" in shown
    # duplicates are refused quietly
    assert "Already remembered" in memory.memory_tool(
        {"action": "remember", "text": "prefers pytest -q"})
    # README documents the folder for other tools
    assert (cwd / ".ai" / "memory" / "README.md").exists()


def test_memory_is_per_agent(cwd):
    scrum.set_agent("alpha")
    memory.remember("alpha's fact")
    scrum.set_agent("beta")
    try:
        assert "alpha's fact" not in memory.read_memory()
        memory.remember("beta's fact")
        assert "beta's fact" in memory.read_memory()
        assert "alpha's fact" in memory.read_memory("alpha")
    finally:
        scrum.set_agent(None)


def test_rewrite_and_clear(cwd, as_agent):
    memory.remember("one")
    assert "rewritten" in memory.memory_tool(
        {"action": "rewrite", "text": "# Memory of w-tester\n- curated"})
    assert memory.read_memory() == "# Memory of w-tester\n- curated"
    assert "cleared" in memory.memory_tool({"action": "rewrite", "text": ""})
    assert memory.read_memory() == ""


def test_memory_cap_refuses_growth(cwd, as_agent):
    memory.rewrite("# Memory of w-tester\n" + "x" * (memory.MAX_CHARS - 100))
    out = memory.remember("y" * 200)
    assert out.startswith("Error") and "rewrite" in out


def test_slug_safety(cwd):
    scrum.set_agent("../../etc <evil>")
    try:
        memory.remember("contained")
        files = list((cwd / ".ai" / "memory").glob("*.md"))
        assert all(".." not in f.name and "/" not in f.name.replace(".md", "")
                   for f in files)
    finally:
        scrum.set_agent(None)


# ---- recall -----------------------------------------------------------------

def _write_thread(cwd, tid, agent, title, messages):
    d = cwd / ".ai" / "threads"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "thread", "id": tid, "title": title,
                         "updated": f"2026-07-19T10:{tid[-2:]}:00", "agent": agent})]
    lines += [json.dumps({"role": r, "content": c}) for r, c in messages]
    (d / f"{tid}.jsonl").write_text("\n".join(lines) + "\n")


def test_recall_list_scopes_to_own_agent(cwd, as_agent):
    _write_thread(cwd, "t-01", "w-tester", "Fix retry bug",
                  [("user", "please fix retries"), ("assistant", "done")])
    _write_thread(cwd, "t-02", "planner", "Sprint planning",
                  [("user", "plan the sprint")])
    mine = recall.recall_tool({"action": "list"})
    assert "t-01" in mine and "t-02" not in mine
    everyone = recall.recall_tool({"action": "list", "all": True})
    assert "t-01" in everyone and "t-02" in everyone


def test_recall_search_and_read(cwd, as_agent):
    _write_thread(cwd, "t-03", "w-tester", "Backoff work",
                  [("user", "add exponential backoff with jitter"),
                   ("assistant", "added retry_call")])
    hits = recall.recall_tool({"action": "search", "query": "jitter"})
    assert "t-03" in hits and "jitter" in hits
    text = recall.recall_tool({"action": "read", "id": "t-03"})
    assert "retry_call" in text and "@w-tester" in text
    assert "no thread matches" in recall.recall_tool({"action": "read", "id": "zz"})
    assert "nothing matches" in recall.recall_tool(
        {"action": "search", "query": "quantum"})


def test_digest_excludes_current_thread(cwd, as_agent):
    _write_thread(cwd, "t-04", "w-tester", "Old work", [("user", "hi")])
    _write_thread(cwd, "t-05", "w-tester", "Live now", [("user", "hi")])
    d = recall.digest(exclude_id="t-05")
    assert d is not None and "Old work" in d and "Live now" not in d
    assert recall.digest(agent="nobody") is None


# ---- registration -----------------------------------------------------------

def test_tools_registered(cwd):
    from goldcomb.tools import TOOLS_BY_NAME
    assert "memory" in TOOLS_BY_NAME and "recall" in TOOLS_BY_NAME
    assert not TOOLS_BY_NAME["memory"].dangerous
    assert not TOOLS_BY_NAME["recall"].dangerous


def test_system_prompt_includes_memory_and_digest(cwd, as_agent):
    memory.remember("golden fact")
    _write_thread(cwd, "t-06", "w-tester", "Earlier session", [("user", "hi")])
    from goldcomb.cli import App
    from goldcomb.config import Config
    cfg = Config.load()
    cfg.settings["tools_enabled"] = True
    app = App.__new__(App)
    app.cfg = cfg
    prompt = App.system_prompt(app)
    assert "golden fact" in prompt
    assert "Earlier session" in prompt
