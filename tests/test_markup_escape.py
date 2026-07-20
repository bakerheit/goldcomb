"""Regression test: exception text containing Rich markup (e.g. '[/dim]')
must never crash the REPL — it is escaped before console.print.

Guards the crash where a MarkupError raised inside the REPL's own
'Unexpected error' handler (because the *original* error message contained
bracket text) killed the whole session.
"""

from unittest.mock import MagicMock, patch

from rich.console import Console

from goldcomb import cli


def test_repl_survives_markup_in_exception_text():
    app = MagicMock()
    app.console = Console(record=True, force_terminal=True, width=80)
    app.cfg.current_provider = "openai"
    app.cfg.current_model = "gpt-4o"
    # The turn blows up with text that looks like a Rich closing tag.
    app.run_turn.side_effect = RuntimeError("closing tag '[/dim]' at position 42")
    app.handle_command.return_value = False  # /exit leaves the loop

    with patch.object(cli, "_build_prompt_session", return_value=None), \
         patch("builtins.input", side_effect=["hello", "/exit"]):
        cli.repl(app)  # must not raise

    out = app.console.export_text()
    assert "Unexpected error" in out
    assert "closing tag '[/dim]' at position 42" in out
