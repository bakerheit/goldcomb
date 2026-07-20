"""Terminal rendering for the REPL — a Claude-Code-style inline live view.

The transcript is *static*: finished messages, tool bullets, and results are
printed once and scroll naturally, so the conversation survives in the
terminal's scrollback after exit. Below it, the Renderer owns a *dynamic
region* — a transient rich ``Live`` holding the working spinner and a
full-width status footer — which is erased and redrawn in place. While the
dynamic region is up, ordinary console prints (tool calls, results, nudges)
render above it, giving the static/dynamic split without an alternate screen.

Both dynamic rows are computed fresh on every refresh, and each refresh
re-reads the terminal size, so the region re-wraps when the window is
resized; :meth:`Renderer.install_resize_handler` hooks SIGWINCH to repaint
immediately instead of waiting for the next tick. In non-interactive contexts
(piped output, one-shot mode) everything degrades to plain, unstyled
streaming.

The look is the goldcomb duotone (see :mod:`goldcomb.theme`): the primary accent
is the model's voice — the ``⏺`` message bullet and the working spinner — and
the secondary accent is the machinery — tool-call bullets and the ``⎿`` result
tree. Colors are read live from the theme module on every repaint (and
interpolated as raw hex so the Renderer works on any Console, themed or not),
so a /theme switch restyles the next repaint immediately.
"""

from __future__ import annotations

import random
import signal
import threading
import time
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text

from . import theme


def _accent() -> str:
    return theme.ACCENT


def _accent2() -> str:
    return theme.ACCENT2


def _warn() -> str:
    return theme.WARN


#: Whimsical gerunds cycled while the model is thinking, à la Claude Code.
WORKING_VERBS = [
    "Thinking", "Pondering", "Cogitating", "Noodling", "Percolating",
    "Ruminating", "Conjuring", "Tinkering", "Scheming", "Musing",
    "Synthesizing", "Wrangling", "Brewing", "Marinating", "Puzzling",
    "Simmering", "Computing", "Deliberating", "Untangling", "Mulling",
]

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def clear_viewport(console: Console) -> None:
    """Take over the window: start the UI at the top of a clean, full-height
    viewport, Claude-Code-style.

    Prior shell output is pushed *into scrollback* (newline padding) rather
    than erased (``ESC[2J`` would destroy it), then the cursor homes to the
    top-left. No-op when not attached to a terminal so piped output stays
    clean — judged by a real isatty() check (honoring an explicit
    ``force_terminal`` override), NOT ``console.is_terminal``: env vars like
    FORCE_COLOR make that True for pipes, which should force colors, never a
    screen takeover.
    """
    forced = console._force_terminal  # None unless force_terminal= was passed
    if forced is False:
        return
    if forced is None:
        try:
            if not console.file.isatty():
                return
        except Exception:  # pragma: no cover - odd file objects
            return
    try:
        height = console.size.height or 24
    except Exception:  # pragma: no cover - size can fail on odd terminals
        height = 24
    console.file.write("\n" * height + "\x1b[H")
    console.file.flush()


#: What the footer callback returns: (left, right) text, or None to hide.
FooterInfo = Callable[[], "tuple[str, str] | None"]


class _StatusRow:
    """Spinner glyph + working verb + elapsed timer, rebuilt at every render."""

    def __init__(self, renderer: "Renderer"):
        self._r = renderer

    def __rich_console__(self, console, options):
        elapsed = int(time.monotonic() - self._r._status_start)
        frame = SPINNER_FRAMES[int(time.monotonic() * 12.5) % len(SPINNER_FRAMES)]
        yield Text.from_markup(
            f"[{_accent()}]{frame}[/] " + self._r._status_text(elapsed)
        )


class _FooterBar:
    """Full-width plum status bar, laid out against the *current* terminal
    width at every render so it reflows when the window is resized."""

    def __init__(self, info: FooterInfo | None):
        self._info = info

    def __rich_console__(self, console, options):
        info = self._info() if self._info else None
        if not info:
            return
        left, right = info
        width = options.max_width
        if len(left) + len(right) + 4 > width:
            right = ""  # too narrow — keep the provider·model side
        if len(left) + 2 > width:
            left = left[: max(0, width - 2)]
        pad = max(1, width - len(left) - len(right) - 2)
        bg, fg, em = theme.TB_BG, theme.TB_FG, theme.TB_EM
        yield Text.assemble(
            (f" {left}", f"bold {em} on {bg}"),
            (" " * pad, f"on {bg}"),
            (f"{right} ", f"{fg} on {bg}"),
        )


class Renderer:
    def __init__(self, console: Console, *, fancy: bool | None = None, markdown: bool = True):
        self.console = console
        # "fancy" = we're attached to a real terminal that can host live
        # regions. Piped or captured output falls back to plain streaming.
        self.fancy = console.is_terminal if fancy is None else fancy
        self.markdown = markdown
        #: Set by the app: returns (left, right) footer text, or None to hide.
        self.footer: FooterInfo | None = None
        self._status: Live | None = None  # transient spinner+footer region
        self._live: Live | None = None  # live markdown region for the current message
        self._buf: list[str] = []
        # A background thread ticks the status region so the spinner animates
        # and the elapsed timer advances while the main thread is busy.
        self._lock = threading.Lock()
        self._spin_stop: threading.Event | None = None
        self._spin_thread: threading.Thread | None = None
        self._status_start = 0.0
        self._status_label = "Thinking"
        self._generic = True
        self._verb_seed = 0

    @property
    def height(self) -> int:
        try:
            return self.console.size.height or 24
        except Exception:  # pragma: no cover - size can fail on odd terminals
            return 24

    # ---- dynamic region: spinner + footer ----------------------------------

    def start_status(self, label: str) -> None:
        self.stop_status()
        self._status_start = time.monotonic()
        self._status_label = label
        self._generic = label.lower() in ("thinking", "working", "")
        self._verb_seed = random.randrange(len(WORKING_VERBS))
        if not self.fancy:
            return
        region = Group(
            _StatusRow(self),
            _FooterBar(lambda: self.footer() if self.footer else None),
        )
        self._status = Live(
            region, console=self.console, transient=True, auto_refresh=False
        )
        self._status.start()
        self._safe_refresh(self._status)
        self._spin_stop = threading.Event()
        self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._spin_thread.start()

    def update_status(self, label: str) -> None:
        # A new explicit label (e.g. "Running run_bash") replaces the verb; the
        # generic "Thinking" just keeps the rotating-verb timer alive.
        if self._status is None:
            self.start_status(label)
            return
        self._status_label = label
        self._generic = label.lower() in ("thinking", "working", "")

    def stop_status(self) -> None:
        if self._spin_stop is not None:
            self._spin_stop.set()
        if self._spin_thread is not None:
            self._spin_thread.join(timeout=0.5)
            self._spin_thread = None
        self._spin_stop = None
        with self._lock:
            if self._status is not None:
                try:
                    self._status.stop()
                except Exception:  # pragma: no cover - teardown must not raise
                    pass
                self._status = None

    def _status_text(self, elapsed: int) -> str:
        if self._generic:
            verb = WORKING_VERBS[(self._verb_seed + elapsed // 4) % len(WORKING_VERBS)]
        else:
            verb = self._status_label
        return (
            f"[{_accent()}]{verb}…[/] "
            f"[dim]({elapsed}s · ctrl-c to interrupt)[/dim]"
        )

    def _spin_loop(self) -> None:
        assert self._spin_stop is not None
        while not self._spin_stop.is_set():
            with self._lock:
                if self._status is not None:
                    self._safe_refresh(self._status)
            self._spin_stop.wait(0.1)

    @staticmethod
    def _safe_refresh(live: Live) -> None:
        try:
            live.refresh()
        except Exception:  # pragma: no cover - never let a repaint crash a turn
            pass

    # ---- resize ------------------------------------------------------------

    def on_resize(self) -> None:
        """Repaint any active live region against the new terminal size."""
        for live in (self._status, self._live):
            if live is not None:
                self._safe_refresh(live)

    def install_resize_handler(self) -> None:
        """Hook SIGWINCH so a window resize repaints immediately.

        prompt_toolkit installs its own handler while a prompt is up, so call
        this at the start of every turn, not just once. No-ops when not
        attached to a terminal, on platforms without SIGWINCH, and outside the
        main thread (signal.signal raises ValueError there).
        """
        if not self.fancy or not hasattr(signal, "SIGWINCH"):
            return
        try:
            signal.signal(signal.SIGWINCH, lambda _sig, _frame: self.on_resize())
        except ValueError:  # pragma: no cover - not in the main thread
            pass

    # ---- assistant message -------------------------------------------------

    def begin_message(self, provider: str, model: str) -> None:
        self.stop_status()
        self.console.print(
            f"[{_accent()}]⏺[/] [dim]{provider} · {model}[/dim]", highlight=False
        )
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
    # These print *statically*. When the dynamic region is up, rich renders
    # them above it, so the spinner + footer stay pinned at the bottom.

    def tool_call(self, summary: str) -> None:
        # escape(): summaries carry model/user text (commands, agent labels
        # like "[worker]") that rich would otherwise eat as markup tags.
        self.console.print(
            f"[{_accent2()}]⏺[/] [bold]{escape(summary)}[/bold]", highlight=False
        )

    def tool_result(self, output: str) -> None:
        """Nest tool output under a ``⎿`` connector, scaled to terminal height."""
        lines = output.splitlines() or ["(no output)"]
        cap = max(3, min(len(lines), self.height // 3))
        for i, ln in enumerate(lines[:cap]):
            connector = f"[dim {_accent2()}]  ⎿  [/]" if i == 0 else "     "
            self.console.print(f"{connector}[dim]{escape(ln)}[/dim]", highlight=False)
        if len(lines) > cap:
            self.console.print(f"[dim]     … +{len(lines) - cap} lines[/dim]")

    def nudge(self, msg: str) -> None:
        self.console.print(f"[{_warn()}]     ↳ {escape(msg)}[/]")

    # ---- misc --------------------------------------------------------------

    def usage(self, usage: dict, session: dict) -> None:
        i, o = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        cached = usage.get("cache_read_input_tokens", 0)
        if i or o or cached:
            # `i` is the uncached remainder, so it reads low once caching is
            # working; the cached figure is what it saved.
            hit = f" · {cached} cached" if cached else ""
            self.console.print(
                f"[dim]     {i} in{hit} · {o} out · session "
                f"{session.get('in', 0)}↑ {session.get('out', 0)}↓[/dim]",
                highlight=False,
            )

    def subagent_start(self, **info) -> None:
        """Sub-agent lifecycle hook (NEXA-3): the NDJSON renderer emits
        ``subagent_start``; the terminal already shows progress via dim lines
        and the spinner, so this is a no-op here."""

    def subagent_end(self, **info) -> None:
        """Paired lifecycle hook — see subagent_start."""

    def stop_all(self) -> None:
        """Tear down any live regions — call on error/interrupt."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # pragma: no cover
                pass
            self._live = None
        self.stop_status()
