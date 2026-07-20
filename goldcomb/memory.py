"""Per-agent persistent memory: one Markdown file per agent, per project.

Lives at ``<project>/.ai/memory/<agent>.md`` — vendor-neutral like the rest of
the ``.ai`` workspace, so any tool (or the user, in an editor) can read and
edit an agent's memory. The file is loaded into that agent's system prompt at
the start of every turn and maintained by the agent itself through the
``memory`` tool: ``remember`` appends one durable fact, ``rewrite`` replaces
the whole file (for pruning/reorganizing), ``show`` prints it.

Identity comes from the acting agent (scrum.CURRENT_AGENT — the same identity
stamped on tickets and threads), so the app's named agents and deployed
sub-agents each accumulate their own file.

Memory is for durable facts: decisions and their reasons, user preferences,
lessons learned, project quirks. It is not a scratchpad — the conversation
itself covers the current session.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import scrum

_DIR = Path(".ai") / "memory"

#: Hard cap per memory file. Memory is prepended to every turn's system
#: prompt, so unbounded growth would silently eat the context window.
MAX_CHARS = 8000

_MEMORY_README = """\
# Agent memory

One Markdown file per agent (`<agent>.md`): durable, agent-maintained notes
loaded into that agent's system prompt each session. Any AI tool may read or
append; keep files small — they ride along on every model call.
"""


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return s or "agent"


def memory_path(agent: str | None = None) -> Path:
    return _DIR / f"{_slug(agent or scrum.CURRENT_AGENT)}.md"


def read_memory(agent: str | None = None) -> str:
    try:
        return memory_path(agent).read_text().strip()
    except OSError:
        return ""


def _write(path: Path, content: str) -> str | None:
    """Write atomically; returns an error message or None."""
    if len(content) > MAX_CHARS:
        return (f"Error: memory would be {len(content)} chars (max {MAX_CHARS}). "
                "Use action='rewrite' with a pruned version instead — keep only "
                "what still matters.")
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        readme = _DIR / "README.md"
        if not readme.exists():
            readme.write_text(_MEMORY_README)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content.rstrip() + "\n")
        tmp.replace(path)
    except OSError as e:
        return f"Error writing memory: {e}"
    return None


def remember(text: str, agent: str | None = None) -> str:
    text = " ".join((text or "").split())
    if not text:
        return "Error: remember requires text=<the fact to keep>."
    who = agent or scrum.CURRENT_AGENT
    path = memory_path(who)
    current = read_memory(who)
    if not current:
        current = f"# Memory of {who}"
    entry = f"- {text}"
    if entry in current.splitlines():
        return f"Already remembered: {text}"
    err = _write(path, current + "\n" + entry)
    if err:
        return err
    return f"Remembered ({path}): {text}"


def rewrite(content: str, agent: str | None = None) -> str:
    who = agent or scrum.CURRENT_AGENT
    path = memory_path(who)
    if not (content or "").strip():
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            return f"Error clearing memory: {e}"
        return f"Memory cleared ({path})."
    err = _write(path, content)
    if err:
        return err
    return f"Memory rewritten ({path}, {len(content)} chars)."


def memory_tool(args: dict) -> str:
    """Entry point for the ``memory`` tool. Never raises."""
    action = str(args.get("action") or "").strip().lower()
    if action == "show":
        current = read_memory()
        return current or f"(no memory yet at {memory_path()} — use action='remember')"
    if action == "remember":
        return remember(str(args.get("text") or ""))
    if action == "rewrite":
        return rewrite(str(args.get("text") or ""))
    return "Error: action must be one of show, remember, rewrite."
