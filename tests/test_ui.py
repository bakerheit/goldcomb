"""Tests for the Claude-Code-style renderer markers and tool tree."""

from rich.console import Console

from goldcomb.theme import ACCENT
from goldcomb.ui import Renderer, WORKING_VERBS


def _plain(record_console: Console) -> str:
    return record_console.export_text()


def test_assistant_and_tool_bullets():
    con = Console(record=True, force_terminal=True, width=70)
    r = Renderer(con, fancy=False)
    r.begin_message("openai", "gpt-4o")
    r.message_delta("hi\n")
    r.end_message()
    r.tool_call("$ ls")
    out = _plain(con)
    assert out.count("⏺") == 2  # one for the message, one for the tool call


def test_tool_result_tree_and_truncation():
    con = Console(record=True, force_terminal=True, width=70, height=24)
    r = Renderer(con, fancy=False)
    r.tool_result("\n".join(f"line{i}" for i in range(50)))
    out = _plain(con)
    assert "⎿" in out                 # tree connector on the first line
    assert "+" in out and "lines" in out  # truncation footer
    # continuation lines are indented to align under the connector
    assert "     line1" in out


def test_empty_output_shows_placeholder():
    con = Console(record=True, force_terminal=True, width=70)
    r = Renderer(con, fancy=False)
    r.tool_result("")
    out = _plain(con)
    assert "⎿" in out and "(no output)" in out


def test_spinner_lifecycle_is_clean():
    import io
    con = Console(force_terminal=True, file=io.StringIO())
    r = Renderer(con, fancy=True)
    r.start_status("Thinking")
    assert r._spin_thread is not None and r._spin_thread.is_alive()
    r.update_status("Running run_bash")
    r.stop_status()
    assert r._spin_thread is None and r._status is None


def test_status_text_rotates_verb_when_generic():
    con = Console(record=True)
    r = Renderer(con, fancy=False)
    r._generic = True
    r._verb_seed = 0
    t0 = r._status_text(0)
    t8 = r._status_text(8)   # 8 // 4 = 2 steps later -> different verb
    assert any(v in t0 for v in WORKING_VERBS)
    assert t0 != t8
    assert ACCENT in t0      # accent-colored


def test_accent_is_a_single_knob():
    # The whole theme keys off one constant.
    assert ACCENT.startswith("#") and len(ACCENT) == 7


def test_footer_bar_lays_out_and_drops_right_when_narrow():
    from goldcomb.ui import _FooterBar

    info = ("kimi · kimi-k3", "⬆1.2k ⬇340 · ctx ~2k · tools")
    con = Console(record=True, force_terminal=True, width=60)
    con.print(_FooterBar(lambda: info))
    out = con.export_text()
    assert "kimi · kimi-k3" in out and "tools" in out

    narrow = Console(record=True, force_terminal=True, width=24)
    narrow.print(_FooterBar(lambda: info))
    out = narrow.export_text()
    assert "kimi · kimi-k3" in out
    assert "ctx" not in out  # right side is dropped, never wrapped

    # No footer callback (or None info) renders nothing and never crashes.
    empty = Console(record=True, force_terminal=True, width=60)
    empty.print(_FooterBar(None))


def test_static_prints_pass_through_active_live_region():
    import io

    con = Console(force_terminal=True, file=io.StringIO(), width=60, height=24)
    r = Renderer(con, fancy=True)
    r.footer = lambda: ("kimi · kimi-k3", "tools")
    r.start_status("Thinking")
    r.tool_call("$ echo hi")  # printed while the dynamic region is up
    r.tool_result("hi")
    r.stop_status()
    out = con.file.getvalue()
    assert "echo hi" in out and "⎿" in out
    assert "kimi · kimi-k3" in out  # footer rendered at least once


def test_tool_call_summary_with_brackets_is_not_eaten_as_markup():
    con = Console(record=True, force_terminal=True, width=100)
    r = Renderer(con, fancy=False)
    r.tool_call("deploy_agent[worker → kimi-k2.6] scan the repo")
    out = con.export_text()
    assert "[worker → kimi-k2.6]" in out


def test_clear_viewport_scrolls_not_erases():
    import io

    from goldcomb.ui import clear_viewport

    con = Console(force_terminal=True, file=io.StringIO(), width=60, height=10)
    clear_viewport(con)
    out = con.file.getvalue()
    assert "\n" * 10 in out          # prior screen pushed into scrollback
    assert "\x1b[H" in out           # cursor homed to top-left
    assert "\x1b[2J" not in out      # never erase — scrollback must survive

    # Not a terminal (piped) → no control codes at all.
    plain = Console(file=io.StringIO(), width=60)
    clear_viewport(plain)
    assert plain.file.getvalue() == ""

    # FORCE_COLOR makes rich's is_terminal True even for pipes; that must
    # force colors, never a screen takeover (a real isatty() decides).
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORCE_COLOR": "3"}):
        piped = Console(file=io.StringIO(), width=60)
        assert piped.is_terminal  # rich says "terminal"…
        clear_viewport(piped)
        assert piped.file.getvalue() == ""  # …but we don't repaint a pipe


def test_on_resize_is_safe_with_and_without_live_regions():
    import io

    con = Console(force_terminal=True, file=io.StringIO(), width=60)
    r = Renderer(con, fancy=True)
    r.on_resize()  # nothing active — must not raise
    r.start_status("Working")
    r.on_resize()  # repaints the active region
    r.stop_status()
    r.on_resize()
