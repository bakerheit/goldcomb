"""Terminal rendering for the REPL — a polished, reactive, height-aware view.

The Renderer owns everything drawn during a turn: a live "working" spinner while
the model thinks and while tools run, markdown-rendered streaming responses, and
tool output previews that shrink to fit short terminals. In non-interactive
contexts (piped output, one-shot mode) it degrades to plain, unstyled streaming
so captured/redirected output stays clean.
"""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown


class Renderer:
    def __init__(self, console: Console, *, fancy: bool | None = None, markdown: bool = True):
        self.console = console
        # "fancy" = we're attached to a real terminal that can host spinners /
        # live regions. Piped or captured output falls back to plain streaming.
        self.fancy = console.is_terminal if fancy is None else fancy
        self.markdown = markdown
        self._status = None  # rich Status (spinner)
        self._live: Live | None = None  # live markdown region for the current message
        self._buf: list[str] = []

    @property
    def height(self) -> int:
        try:
            return self.console.size.height or 24
        except Exception:  # pragma: no cover - size can fail on odd terminals
            return 24

    # ---- working spinner ---------------------------------------------------

    def start_status(self, label: str) -> None:
        self.stop_status()
        if self.fancy:
            self._status = self.console.status(
                f"[cyan]{label}…[/cyan] [dim](ctrl-c to interrupt)[/dim]", spinner="dots"
            )
            self._status.start()

    def update_status(self, label: str) -> None:
        if self._status is not None:
            self._status.update(f"[cyan]{label}…[/cyan] [dim](ctrl-c to interrupt)[/dim]")
        else:
            self.start_status(label)

    def stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # ---- assistant message -------------------------------------------------

    def begin_message(self, provider: str, model: str) -> None:
        self.stop_status()
        self.console.print(f"[bold cyan]●[/bold cyan] [dim]{provider} · {model}[/dim]")
        self._buf = []
        if self.fancy and self.markdown:
            # vertical_overflow="visible" lets a long response scroll naturally
            # instead of being clipped to the live region.
            self._live = Live(
                console=self.console, refresh_per_second=12, vertical_overflow="visible"
            )
            self._live.start()

    def message_delta(self, text: str) -> None:
        self._buf.append(text)
        if self._live is not None:
            self._live.update(Markdown("".join(self._buf)))
        else:
            self.console.file.write(text)
            self.console.file.flush()

    def end_message(self) -> None:
        if self._live is not None:
            self._live.update(Markdown("".join(self._buf)))
            self._live.stop()
            self._live = None
        else:
            self.console.file.write("\n")
            self.console.file.flush()

    # ---- tools -------------------------------------------------------------

    def tool_call(self, summary: str) -> None:
        self.stop_status()
        self.console.print(f"[magenta]⚙[/magenta] {summary}", highlight=False)

    def tool_result(self, output: str) -> None:
        """Show a preview whose length scales with the terminal height."""
        lines = output.splitlines()
        cap = max(4, min(len(lines), self.height // 3))
        for ln in lines[:cap]:
            self.console.print(f"[dim]  {ln}[/dim]", highlight=False)
        if len(lines) > cap:
            self.console.print(f"[dim]  … (+{len(lines) - cap} more lines)[/dim]")

    def nudge(self, msg: str) -> None:
        self.console.print(f"[yellow]  ↳ {msg}[/yellow]")

    # ---- misc --------------------------------------------------------------

    def usage(self, usage: dict, session: dict) -> None:
        i, o = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        if i or o:
            self.console.print(
                f"[dim]  ↳ {i} in / {o} out · session {session.get('in', 0)}↑ "
                f"{session.get('out', 0)}↓[/dim]"
            )

    def stop_all(self) -> None:
        """Tear down any live regions — call on error/interrupt."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # pragma: no cover
                pass
            self._live = None
        self.stop_status()
