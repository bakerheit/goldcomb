"""Tests for the scrum board tool (goldcomb/scrum.py).

The board persists to ./.ai/scrum/board.json relative to the CWD, so each
test runs in a tmp dir via the ``cwd`` fixture.
"""

import json
import re

import pytest

import goldcomb.scrum as scrum


@pytest.fixture()
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _board(cwd):
    return json.loads((cwd / ".ai" / "scrum" / "board.json").read_text())


def _ticket(message: str) -> str:
    """Pull the JIRA-style ticket id (KEY-N) out of an 'Added ...' message."""
    m = re.search(r"\b[A-Z][A-Z0-9]{0,5}-\d+\b", message)
    assert m, f"no ticket id in: {message!r}"
    return m.group(0)


def _make_story_with_task(cwd):
    """init + epic + story + task; return (epic_id, story_id, task_id)."""
    assert "Initialized" in scrum.scrum({"action": "init", "project": "demo"})
    eid = _ticket(scrum.scrum({"action": "epic_add", "title": "Backend"}))
    sid = _ticket(scrum.scrum({"action": "story_add", "title": "Add API", "epic": eid}))
    tid = _ticket(scrum.scrum(
        {"action": "task_add", "story": sid, "title": "Write endpoint", "points": 3}))
    return eid, sid, tid


def test_no_board_prompts_init(cwd):
    out = scrum.scrum({"action": "show"})
    assert "No scrum board yet" in out
    assert "init" in out


def test_unknown_action(cwd):
    out = scrum.scrum({"action": "dance"})
    assert "unknown action" in out
    assert "epic_add" in out  # lists valid actions


def test_missing_action(cwd):
    assert "action is required" in scrum.scrum({})


def test_init_creates_board_file(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    data = _board(cwd)
    assert data["meta"]["project"] == "demo"
    assert data["meta"]["key"] == "DEMO"  # derived from the name
    assert data["stories"] == {}
    assert data["sprint"] is None


def test_init_custom_key_and_derivation(cwd):
    out = scrum.scrum({"action": "init", "project": "demo", "key": "core"})
    assert "[CORE]" in out
    assert _board(cwd)["meta"]["key"] == "CORE"
    assert scrum.derive_key("payment gateway service") == "PGS"
    assert scrum.derive_key("goldcomb") == "GOLD"
    assert scrum.derive_key("") == "TCK"


def test_ticket_ids_share_one_jira_sequence(cwd):
    eid, sid, tid = _make_story_with_task(cwd)
    assert (eid, sid, tid) == ("DEMO-1", "DEMO-2", "DEMO-3")


def test_in_progress_autoassigns_current_agent(cwd, monkeypatch):
    _eid, sid, tid = _make_story_with_task(cwd)
    monkeypatch.setattr(scrum, "CURRENT_AGENT", "refactorer")
    out = scrum.scrum({"action": "task_update", "task": tid, "status": "in_progress"})
    assert "assignee->refactorer" in out
    task = _board(cwd)["stories"][sid]["tasks"][0]
    assert task["assignee"] == "refactorer"
    # The board's show output surfaces who works on what.
    shown = scrum.scrum({"action": "show"})
    assert "In progress:" in shown and f"{tid}" in shown and "@refactorer" in shown


def test_assign_action(cwd, monkeypatch):
    _eid, sid, tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "assign", "ticket": tid, "assignee": "alice"})
    # Bare labels resolve to their roster person (label kept in parens), so
    # board identity matches whoever a deploy of that label becomes.
    assert f"Assigned {tid} to" in out and "(alice)" in out
    # in_progress keeps an existing assignee rather than stealing the ticket.
    monkeypatch.setattr(scrum, "CURRENT_AGENT", "bob")
    scrum.scrum({"action": "task_update", "task": tid, "status": "in_progress"})
    assert _board(cwd)["stories"][sid]["tasks"][0]["assignee"].endswith("(alice)")
    # Stories are tickets too; assign defaults to the current agent.
    assert f"Assigned {sid} to bob" in scrum.scrum({"action": "assign", "ticket": sid})
    assert "Error: no ticket" in scrum.scrum({"action": "assign", "ticket": "DEMO-99"})


def test_epic_story_task_flow_persists(cwd):
    eid, sid, tid = _make_story_with_task(cwd)
    data = _board(cwd)
    assert eid in data["epics"]
    assert sid in data["stories"]
    assert sid in data["epics"][eid]["stories"]
    task = data["stories"][sid]["tasks"][0]
    assert task["id"] == tid
    assert task["status"] == "todo"
    assert task["points"] == 3


def test_story_add_without_epic(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    out = scrum.scrum({"action": "story_add", "title": "Loose story"})
    assert "no epic" in out
    assert len(_board(cwd)["stories"]) == 1


def test_story_add_bad_epic_rejected(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    out = scrum.scrum({"action": "story_add", "title": "x", "epic": "E9-nope"})
    assert "Error: no epic" in out
    assert _board(cwd)["stories"] == {}


def test_task_status_transitions(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    assert "in_progress" in scrum.scrum(
        {"action": "task_update", "task": tid, "status": "in_progress"})
    assert "done" in scrum.scrum({"action": "task_update", "task": tid, "status": "done"})
    task = _board(cwd)["stories"][sid]["tasks"][0]
    assert task["status"] == "done"


def test_invalid_transition_refused(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    # todo -> done is allowed, but blocked -> done is not.
    scrum.scrum({"action": "task_update", "task": tid, "status": "blocked"})
    out = scrum.scrum({"action": "task_update", "task": tid, "status": "done"})
    assert "not a valid transition" in out


def test_bad_status_rejected(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "task_update", "task": tid, "status": "doing"})
    assert "must be one of" in out


def test_task_update_unknown_task(cwd):
    _make_story_with_task(cwd)
    assert "Error: no task" in scrum.scrum(
        {"action": "task_update", "task": "T9-nope", "status": "done"})


def test_story_points_rollup(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "task_update", "task": tid, "status": "done"})
    out = scrum.scrum({"action": "story_show", "story": sid})
    assert "points: 3/3" in out


def test_sprint_lifecycle(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    assert "Started sprint 1" in scrum.scrum({"action": "sprint_start", "goal": "Ship API"})
    assert f"Added {sid}" in scrum.scrum({"action": "sprint_add", "story": sid})
    # can't start a second sprint while one is active
    assert "already active" in scrum.scrum({"action": "sprint_start"})
    scrum.scrum({"action": "task_update", "task": tid, "status": "done"})
    status = scrum.scrum({"action": "sprint_status"})
    assert "3/3 points done" in status
    end = scrum.scrum({"action": "sprint_end"})
    assert "Sprint 1 ended" in end
    assert _board(cwd)["sprint"]["active"] is False


def test_sprint_add_requires_active_sprint(cwd):
    _eid, sid, _tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "sprint_add", "story": sid})
    assert "no active sprint" in out


def test_task_list_filter(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "task_add", "story": sid, "title": "Second"})
    todo = scrum.scrum({"action": "task_list", "status": "todo"})
    assert "Second" in todo
    done = scrum.scrum({"action": "task_list", "status": "done"})
    assert "no matching tasks" in done


def test_corrupt_board_file_recovers(cwd):
    (cwd / ".ai" / "scrum").mkdir(parents=True)
    (cwd / ".ai" / "scrum" / "board.json").write_text("{ not json")
    out = scrum.scrum({"action": "show"})
    assert "No scrum board yet" in out


def test_legacy_board_migrates_into_ai(cwd):
    legacy = scrum.new_board("old-project")
    legacy["stories"]["S1-keep"] = {
        "title": "Keep me", "priority": "high", "notes": "", "tasks": [],
    }
    (cwd / ".nexais").mkdir()
    (cwd / ".nexais" / "board.json").write_text(json.dumps(legacy))

    out = scrum.scrum({"action": "show"})
    assert "old-project" in out and "Keep me" in out
    # Migrated into .ai/scrum, old file preserved as a backup, not deleted.
    assert (cwd / ".ai" / "scrum" / "board.json").exists()
    assert not (cwd / ".nexais" / "board.json").exists()
    assert (cwd / ".nexais" / "board.json.migrated").exists()
    # And the migrated board is now the live one.
    assert "S1-keep" in _board(cwd)["stories"]
    # Old ids stay; NEW items on a legacy board get JIRA-style tickets.
    out = scrum.scrum({"action": "task_add", "story": "S1-keep", "title": "modern"})
    assert _ticket(out) == "OP-1"  # key derived from "old-project"


def test_save_board_writes_format_readme(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    readme = cwd / ".ai" / "scrum" / "README.md"
    assert readme.exists() and "board.json" in readme.read_text()


def test_opt_in_gating(cwd):
    from goldcomb.agents import subagent_tools
    from goldcomb.tools import available_tools, tool_specs

    def offered():
        return [t.name for t in available_tools()]

    # No board → the model is never offered the scrum tool.
    assert not scrum.is_enabled()
    assert "scrum" not in offered()
    assert "scrum" not in [s.name for s in tool_specs()]
    assert "scrum" not in [t.name for t in subagent_tools()]

    out = scrum.enable()
    assert "Scrum tracking on" in out and "new board" in out
    assert scrum.is_enabled()
    assert "scrum" in offered()
    assert "scrum" in [t.name for t in subagent_tools()]

    assert "board kept" in scrum.disable()
    assert not scrum.is_enabled()
    assert "scrum" not in offered()
    # The off switch keeps the data.
    assert (cwd / ".ai" / "scrum" / "board.json").exists()

    # Re-enabling reuses the same board rather than starting over.
    assert "existing board" in scrum.enable()


def test_disabled_board_refuses_model_actions(cwd):
    scrum.enable()
    scrum.scrum({"action": "epic_add", "title": "Work"})
    scrum.disable()
    out = scrum.scrum({"action": "show"})
    assert "switched off" in out and "/scrum on" in out


def test_scrum_slash_command(cwd):
    from pathlib import Path

    from rich.console import Console

    from goldcomb.cli import App
    from goldcomb.config import Config

    cfg = Config({"providers": {}, "current": {}, "settings": {}}, Path("/dev/null"))
    app = App(cfg, Console(record=True, force_terminal=True, width=100))
    app.handle_command("/scrum")        # off by default
    app.handle_command("/scrum on")     # creates the board
    app.handle_command("/scrum")        # now prints the board
    app.handle_command("/scrum off")
    out = app.console.export_text()
    assert "off for this project" in out
    assert "Scrum tracking on" in out
    assert "Board:" in out
    assert "board kept" in out


def test_describe_call_summary(cwd):
    from goldcomb.tools import describe_call
    assert describe_call(
        "scrum", {"action": "task_update", "task": "T1-x"}) == "scrum task_update T1-x"
    assert describe_call("scrum", {"action": "show"}) == "scrum show"


def test_scrum_tool_registered_and_safe():
    from goldcomb.tools import TOOLS_BY_NAME
    tool = TOOLS_BY_NAME.get("scrum")
    assert tool is not None
    assert tool.dangerous is False  # board writes shouldn't need confirmation
    assert "action" in tool.spec.parameters["required"]


# ---------------------------------------------------------------------------
# v3 features: delete, comments, find, ticket_show, history, labels, deps
# ---------------------------------------------------------------------------

def test_task_del(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    assert f"Deleted task {tid}" in scrum.scrum({"action": "task_del", "task": tid})
    assert "no task" in scrum.scrum({"action": "task_del", "task": tid})
    assert _board(cwd)["stories"][sid]["tasks"] == []


def test_story_del_refuses_with_tasks_unless_forced(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "story_del", "story": sid})
    assert out.startswith("Refused:") and tid in out
    assert sid in _board(cwd)["stories"]
    assert "Deleted story" in scrum.scrum(
        {"action": "story_del", "story": sid, "force": True})
    board = _board(cwd)
    assert sid not in board["stories"]
    assert all(sid not in e["stories"] for e in board["epics"].values())


def test_story_del_removes_from_sprint(cwd):
    _eid, sid, _tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "sprint_start", "goal": "ship"})
    scrum.scrum({"action": "sprint_add", "story": sid})
    scrum.scrum({"action": "story_del", "story": sid, "force": True})
    assert sid not in _board(cwd)["sprint"]["stories"]


def test_epic_del_keeps_stories(cwd):
    eid, sid, _tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "epic_del", "epic": eid})
    assert out.startswith("Refused:")
    out = scrum.scrum({"action": "epic_del", "epic": eid, "force": True})
    assert "un-grouped" in out
    board = _board(cwd)
    assert eid not in board["epics"]
    assert sid in board["stories"]  # story survives, just epic-less


def test_comment_and_ticket_show(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    scrum.set_agent("reviewer")
    try:
        assert "Commented" in scrum.scrum(
            {"action": "comment", "ticket": tid, "text": "looks flaky"})
    finally:
        scrum.set_agent(None)
    out = scrum.scrum({"action": "ticket_show", "ticket": tid})
    assert "@reviewer" in out and "looks flaky" in out
    # story + epic ids resolve through the same action
    assert "tasks:" in scrum.scrum({"action": "ticket_show", "ticket": sid})
    assert "no ticket" in scrum.scrum({"action": "comment", "ticket": "NOPE-1",
                                       "text": "x"})


def test_find(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    out = scrum.scrum({"action": "find", "query": "endpoint"})
    assert tid in out
    assert "nothing matches" in scrum.scrum({"action": "find", "query": "zzz"})
    assert "requires query" in scrum.scrum({"action": "find"})


def test_history_records_mutations(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "task_update", "task": tid, "status": "in_progress"})
    out = scrum.scrum({"action": "history"})
    assert "task_update" in out and "epic_add" in out
    # history survives a reload (it is part of the persisted board)
    assert _board(cwd)["history"]


def test_labels_roundtrip_and_filter(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    assert "labels->" in scrum.scrum(
        {"action": "task_update", "task": tid, "labels": "Bug, ui , bug"})
    assert _board(cwd)["stories"]
    out = scrum.scrum({"action": "task_list", "label": "bug"})
    assert tid in out and "#bug" in out
    assert "(no matching tasks)" in scrum.scrum({"action": "task_list", "label": "perf"})


def test_blocked_by_gates_done(cwd):
    _eid, sid, tid = _make_story_with_task(cwd)
    other = _ticket(scrum.scrum(
        {"action": "task_add", "story": sid, "title": "Write tests"}))
    assert "blocked_by->" in scrum.scrum(
        {"action": "task_update", "task": tid, "blocked_by": other})
    out = scrum.scrum({"action": "task_update", "task": tid, "status": "done"})
    assert out.startswith("Refused:") and other in out
    scrum.scrum({"action": "task_update", "task": other, "status": "done"})
    assert "status->done" in scrum.scrum(
        {"action": "task_update", "task": tid, "status": "done"})


def test_blocked_by_rejects_unknown_and_self(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    assert "unknown ticket" in scrum.scrum(
        {"action": "task_update", "task": tid, "blocked_by": "NOPE-9"})
    assert "cannot block itself" in scrum.scrum(
        {"action": "task_update", "task": tid, "blocked_by": tid})


def test_ticket_add_one_call(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    out = scrum.scrum({"action": "ticket_add", "title": "Fix login",
                       "points": 3, "labels": "bug,auth"})
    assert "Filed" in out and "created" in out  # Backlog epic made on the fly
    board = _board(cwd)
    assert len(board["epics"]) == 1
    epic = next(iter(board["epics"].values()))
    assert epic["title"] == "Backlog" and len(epic["stories"]) == 1
    sid = epic["stories"][0]
    task = board["stories"][sid]["tasks"][0]
    assert task["title"] == "Fix login" and task["points"] == 3
    assert task["labels"] == ["bug", "auth"]
    # second ticket reuses the existing epic
    out = scrum.scrum({"action": "ticket_add", "title": "Add logout"})
    assert "created" not in out
    assert len(_board(cwd)["epics"]) == 1


def test_ticket_add_into_sprint(cwd):
    scrum.scrum({"action": "init", "project": "demo"})
    scrum.scrum({"action": "sprint_start", "goal": "auth"})
    out = scrum.scrum({"action": "ticket_add", "title": "Fix login", "sprint": True})
    assert "added to sprint 1" in out
    board = _board(cwd)
    assert board["sprint"]["stories"]  # the new story is in
    out = scrum.scrum({"action": "ticket_add", "title": "X", "epic": "NOPE-1"})
    assert "no epic" in out


def test_sprint_remove(cwd):
    _eid, sid, _tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "sprint_start", "goal": "g"})
    scrum.scrum({"action": "sprint_add", "story": sid})
    assert f"Removed {sid}" in scrum.scrum({"action": "sprint_remove", "story": sid})
    assert sid not in _board(cwd)["sprint"]["stories"]
    assert "is not in sprint" in scrum.scrum({"action": "sprint_remove", "story": sid})


def test_heartbeat_requires_a_board(cwd):
    scrum.heartbeat("w-1")   # no board yet: silent no-op
    assert not (cwd / ".ai" / "scrum" / "heartbeats.json").exists()
    assert scrum.agent_heartbeats() == {}


def test_heartbeat_records_and_prunes(cwd):
    import json as _json
    import time as _time
    scrum.scrum({"action": "init", "project": "demo"})
    scrum.heartbeat("w-1")
    scrum.heartbeat()        # default: current identity
    beats = scrum.agent_heartbeats()
    assert "w-1" in beats and "goldcomb" in beats
    assert beats["w-1"] == pytest.approx(_time.time(), abs=5)
    # ancient entries get pruned on the next beat
    path = cwd / ".ai" / "scrum" / "heartbeats.json"
    stale = scrum.agent_heartbeats()
    stale["ghost"] = _time.time() - 100_000
    path.write_text(_json.dumps(stale))
    scrum.heartbeat("w-1")
    assert "ghost" not in scrum.agent_heartbeats()
    # corrupt sidecar degrades to empty, never raises
    path.write_text("{nope")
    assert scrum.agent_heartbeats() == {}
    scrum.heartbeat("w-2")   # and the next beat rewrites it cleanly
    assert "w-2" in scrum.agent_heartbeats()


def test_assign_resolves_labels_through_roster(cwd):
    _eid, _sid, tid = _make_story_with_task(cwd)
    scrum.scrum({"action": "assign", "ticket": tid, "assignee": "swift-worker-2"})
    board = _board(cwd)
    task = [t for st in board["stories"].values()
            for t in st["tasks"] if t["id"] == tid][0]
    # the raw label became a roster person, suffix preserved
    assert task["assignee"].endswith("(swift-worker-2)")
    from goldcomb.names import looks_human
    assert looks_human(task["assignee"].split(" (")[0])
    # ...and the SAME person a deploy of that label resolves to
    from goldcomb.names import humanize
    assert humanize("swift-worker-2") == task["assignee"]
    # human names assign untouched
    scrum.scrum({"action": "assign", "ticket": tid, "assignee": "Maya Wilder"})
    task = [t for st in _board(cwd)["stories"].values()
            for t in st["tasks"] if t["id"] == tid][0]
    assert task["assignee"] == "Maya Wilder"
