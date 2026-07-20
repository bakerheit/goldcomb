"""The goldcomb look — one module owns every color and style in the app.

Colors come from swappable *themes*. Each theme is a "duotone" — a primary
accent for the model's voice (message bullets, the spinner, the wordmark,
whatever is *current*: active provider, selected model, resumed thread) and a
secondary accent for the machinery (tool calls, slash commands, numbered
pick-lists) — plus a bottom-toolbar palette. Green/amber/red are reserved for
outcomes — success, caution, failure — and are the same across themes so an
error is always red and a ✓ always green.

``THEMES`` holds the catalog; ``apply_theme`` installs one into the process-
wide module constants (``ACCENT``, ``THEME``, ``TB_BG``, ...) that the rest of
the app imports, and the configured theme is applied at import time so any
import order sees it. Changing the theme rebuilds ``THEME`` in place, so
consoles already created via :func:`make_console` pick it up live. A console
without the theme renders semantic names (``[ok]…[/ok]``) unstyled rather than
crashing, so library use stays safe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from rich.console import Console
from rich.theme import Theme

# ---- the catalog ------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "goldcomb": {
        "blurb": "golden orange + honey amber · the goldcomb default",
        "accent": "#e8a33d",
        "accent2": "#ffc14d",
        "tb_bg": "#2e2214",
        "tb_fg": "#d9b47c",
        "tb_em": "#ffdda0",
    },
    "aurora": {
        "blurb": "iris violet + aurora teal · the default",
        "accent": "#7d5fff",
        "accent2": "#14b8a6",
        "tb_bg": "#221b3a",
        "tb_fg": "#a99df0",
        "tb_em": "#d4ccff",
    },
    "ember": {
        "blurb": "campfire orange + ember gold",
        "accent": "#ff6b35",
        "accent2": "#ffb703",
        "tb_bg": "#33201a",
        "tb_fg": "#f0b49d",
        "tb_em": "#ffd9c4",
    },
    "ocean": {
        "blurb": "deep sea blue + crest cyan",
        "accent": "#38bdf8",
        "accent2": "#2dd4bf",
        "tb_bg": "#16283a",
        "tb_fg": "#9dc3e8",
        "tb_em": "#cde9ff",
    },
    "sakura": {
        "blurb": "blossom pink + fresh green",
        "accent": "#f472b6",
        "accent2": "#34d399",
        "tb_bg": "#3a1d30",
        "tb_fg": "#eeb0d3",
        "tb_em": "#ffd5ec",
    },
    "mono": {
        "blurb": "soft white + slate gray · nearly monochrome",
        "accent": "#e2e8f0",
        "accent2": "#94a3b8",
        "tb_bg": "#2b2f36",
        "tb_fg": "#aab0ba",
        "tb_em": "#e2e8f0",
    },
    "matrix": {
        "blurb": "phosphor green + bright green",
        "accent": "#4ade80",
        "accent2": "#22c55e",
        "tb_bg": "#0f2415",
        "tb_fg": "#8fd6a4",
        "tb_em": "#c4f5d4",
    },
}

DEFAULT_THEME = "goldcomb"

# Outcomes — constant across themes (see module docstring).
OK = "#10b981"
WARN = "#f59e0b"
ERR = "#ef4444"

# ---- process-wide theme state ----------------------------------------------
# Everything else imports these constants; apply_theme() rewrites them (and
# rebuilds THEME in place) so a /theme switch restyles the live app.

ACCENT = THEMES[DEFAULT_THEME]["accent"]
ACCENT2 = THEMES[DEFAULT_THEME]["accent2"]
TB_BG = THEMES[DEFAULT_THEME]["tb_bg"]
TB_FG = THEMES[DEFAULT_THEME]["tb_fg"]
TB_EM = THEMES[DEFAULT_THEME]["tb_em"]
CURRENT_THEME = DEFAULT_THEME


def _build_theme() -> Theme:
    return Theme(
        {
            "accent": ACCENT,
            "accent2": ACCENT2,
            "ok": OK,
            "warn": WARN,
            "err": ERR,
            # Slash commands and other things the user can type.
            "cmd": f"bold {ACCENT2}",
            # Indices in numbered pick-lists (/models, menus).
            "num": ACCENT2,
            # The active/current selection (→ and ← markers, resumed thread ids).
            "cur": f"bold {ACCENT}",
            # Help-section headings. NOTE: theme names are safe in *markup* on
            # any console (unknown tags degrade to unstyled text), but API-level
            # style parameters (border_style=, header_style=) raise MissingStyle
            # on an unthemed console — pass literal definitions built from the
            # constants there instead (e.g. f"bold {ACCENT}").
            "heading": f"bold {ACCENT}",
            # Inline code in rendered markdown follows the machinery accent
            # rather than rich's default cyan-on-black.
            "markdown.code": f"bold {ACCENT2}",
        }
    )


THEME = _build_theme()


def apply_theme(name: str) -> bool:
    """Install ``name`` as the process-wide theme. Returns False if unknown."""
    global ACCENT, ACCENT2, TB_BG, TB_FG, TB_EM, CURRENT_THEME
    spec = THEMES.get(name)
    if spec is None:
        return False
    ACCENT = spec["accent"]
    ACCENT2 = spec["accent2"]
    TB_BG = spec["tb_bg"]
    TB_FG = spec["tb_fg"]
    TB_EM = spec["tb_em"]
    CURRENT_THEME = name
    # Mutate in place: Consoles created earlier hold a reference to THEME, so
    # replacing the object would leave them on the old colors.
    THEME.styles.clear()
    THEME.styles.update(_build_theme().styles)
    return True


# ---- startup resolution ------------------------------------------------------


def _theme_from_config() -> str | None:
    """Read settings.theme straight from the config file.

    config.py imports nothing from here, but reading the file directly keeps
    theme a pure config-side setting without an import cycle. Any failure
    (missing file, bad JSON) just means "no configured theme".
    """
    env = os.environ.get("GOLDCOMB_CONFIG_DIR")
    if env:
        base = Path(env).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "goldcomb"
    try:
        data = json.loads((base / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("settings", {}).get("theme")
    return name if name in THEMES else None


apply_theme(os.environ.get("GOLDCOMB_THEME") or _theme_from_config() or DEFAULT_THEME)


def make_console(**kwargs) -> Console:
    """A Console wired to the goldcomb theme.

    Auto-highlighting is off: rich's default highlighter recolors digits,
    paths, and /slashes ad hoc, which fights a deliberate palette. Every color
    in the app comes from the theme.
    """
    kwargs.setdefault("highlight", False)
    return Console(theme=THEME, **kwargs)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def gradient(text: str, start: str | None = None, end: str | None = None) -> str:
    """Markup that fades ``text`` from ``start`` to ``end``, letter by letter.

    Defaults follow the *current* theme at call time.
    """
    start = start or ACCENT
    end = end or ACCENT2
    r0, g0, b0 = _hex_to_rgb(start)
    r1, g1, b1 = _hex_to_rgb(end)
    steps = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        t = i / steps
        r, g, b = (
            round(r0 + (r1 - r0) * t),
            round(g0 + (g1 - g0) * t),
            round(b0 + (b1 - b0) * t),
        )
        out.append(f"[#{r:02x}{g:02x}{b:02x}]{ch}[/]")
    return "".join(out)
