"""Tests for the ask_user tool: registry, terminal flow, and serve flow."""

import queue
from pathlib import Path

from rich.console import Console

from goldcomb import agents
from goldcomb.cli import App, _resolve_answer, _valid_questions
from goldcomb.config import Config
from goldcomb.server import make_serve_app
from goldcomb.tools import TOOLS_BY_NAME, set_ask_runner

QUESTIONS = {
    "questions": [
        {
            "question": "Which database?",
            "header": "Database",
            "options": [
                {"label": "Postgres", "description": "relational, battle-tested"},
                {"label": "SQLite", "description": "embedded, zero-config"},
                {"label": "Redis"},
            ],
        },
        {
            "question": "Which features?",
            "multi_select": True,
            "options": [{"label": "auth"}, {"label": "billing"}, {"label": "search"}],
        },
    ]
}


def make_app(monkeypatch=None):
    cfg = Config({"providers": {}, "current": {}, "settings": {}}, Path("/dev/null"))
    return App(cfg, Console(record=True, force_terminal=True, width=80))


# ---- registry ---------------------------------------------------------------


def test_ask_user_registered_but_not_for_subagents():
    tool = TOOLS_BY_NAME["ask_user"]
    assert not tool.dangerous  # asking is safe — never needs confirmation
    assert "ask_user" not in [t.name for t in agents.subagent_tools()]


# ---- helpers ----------------------------------------------------------------


def test_resolve_answer_maps_numbers_to_labels():
    options = QUESTIONS["questions"][0]["options"]
    assert _resolve_answer("2", options) == "SQLite"
    assert _resolve_answer("1,3", options) == "Postgres, Redis"
    assert _resolve_answer("postgres please", options) == "postgres please"
    assert _resolve_answer("9", options) == "9"  # out of range → literal text
    assert "decide yourself" in _resolve_answer("", options)


def test_valid_questions_filters_and_caps():
    qs = _valid_questions({"questions": [
        {"question": "a?"}, {"question": "  "}, "junk",
        {"question": "b?"}, {"question": "c?"}, {"question": "d?"}, {"question": "e?"},
    ]})
    assert [q["question"] for q in qs] == ["a?", "b?", "c?", "d?"]
    assert _valid_questions({"questions": "nope"}) == []


# ---- terminal flow ----------------------------------------------------------


def test_terminal_ask_flow(monkeypatch):
    app = make_app()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    replies = iter(["2", "1,3"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    out = app._ask_user_impl(QUESTIONS)
    assert "Q: Which database?\nA: SQLite" in out
    assert "Q: Which features?\nA: auth, search" in out
    printed = app.console.export_text()
    assert "Which database?" in printed and "Postgres" in printed


def test_terminal_ask_flow_noninteractive(monkeypatch):
    app = make_app()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    out = app._ask_user_impl(QUESTIONS)
    assert out.startswith("Error:") and "best judgment" in out


def test_ask_runner_is_wired_to_the_app(monkeypatch):
    app = make_app()  # __init__ registers the runner
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    out = TOOLS_BY_NAME["ask_user"].run(QUESTIONS)
    assert "best judgment" in out
    set_ask_runner(None)
    out = TOOLS_BY_NAME["ask_user"].run(QUESTIONS)
    assert "no interactive user" in out
    set_ask_runner(app._ask_user_impl)  # restore for other tests


# ---- serve flow -------------------------------------------------------------


def test_serve_ask_roundtrip():
    cfg = Config({"providers": {}, "current": {}, "settings": {}}, Path("/dev/null"))
    events = []
    commands = queue.Queue()
    commands.put({"type": "sudo", "on": True})  # noise → protocol error, stays open
    commands.put({"type": "answer", "answers": ["Postgres", ""]})
    app = make_serve_app(
        cfg, Console(record=True, force_terminal=False), events.append, commands
    )
    out = app._ask_user_impl(QUESTIONS)
    assert "Q: Which database?\nA: Postgres" in out
    assert "Q: Which features?\nA: (no answer — decide yourself)" in out
    kinds = [e["event"] for e in events]
    assert kinds == ["ask_request", "error"]
    assert len(events[0]["questions"]) == 2


def test_serve_ask_disconnect_degrades():
    cfg = Config({"providers": {}, "current": {}, "settings": {}}, Path("/dev/null"))
    commands = queue.Queue()
    commands.put(None)  # EOF
    app = make_serve_app(
        cfg, Console(record=True, force_terminal=False), lambda e: None, commands
    )
    out = app._ask_user_impl(QUESTIONS)
    assert "disconnected" in out
