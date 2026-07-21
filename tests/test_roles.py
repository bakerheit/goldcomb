"""Tests for agent roles (goldcomb/roles.py) and the --role plumbing."""

from goldcomb.roles import ROLES, role_prompt


def test_planner_role_exists_and_is_board_shaped():
    prompt = role_prompt("planner")
    assert prompt is not None
    for needle in ("scrum", "ticket", "sprint", "assign"):
        assert needle in prompt.lower()
    # planners delegate, not implement
    assert "do NOT implement" in prompt


def test_known_roles_yield_their_rich_persona():
    # Case/space-insensitive; planner and advisor keep their built-in blocks.
    assert role_prompt(" Planner ") == ROLES["planner"]
    assert role_prompt("advisor") == ROLES["advisor"]


def test_free_text_role_is_injected_as_is():
    # Any other non-empty text is a free-text role description (the unified
    # role field), used verbatim — not rejected.
    block = role_prompt("Backend engineer")
    assert block is not None
    assert "Backend engineer" in block


def test_empty_role_yields_no_block():
    assert role_prompt(None) is None
    assert role_prompt("") is None
    assert role_prompt("   ") is None


def test_team_context_block_in_system_prompt():
    from goldcomb.cli import App
    from goldcomb.config import Config

    cfg = Config.load()
    cfg.settings["tools_enabled"] = False
    cfg.settings["team"] = "Your lead: @planner. Your reports: @worker-a."
    app = App.__new__(App)
    app.cfg = cfg
    prompt = App.system_prompt(app)
    assert "Team context" in prompt
    assert "@worker-a" in prompt
