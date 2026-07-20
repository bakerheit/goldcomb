"""Per-project conversation threads — persistent, resumable sessions à la Claude Code.

Threads live under the global config directory, namespaced by working directory,
so a project's history is available whenever you return to that project without
leaving files scattered in the repo::

    <config_dir>/projects/<cwd-key>/threads/<thread-id>.json

That copy is the canonical store: it keeps every provider-internal field
(tool calls, tool results) so a resumed conversation replays exactly.

Alongside it, every save is exported in a vendor-neutral **interchange format**
so *any* AI tool — not just goldcomb frontends — can read a project's chat
history and contribute its own::

    <project>/.ai/threads/<thread-id>.jsonl

One JSON object per line. Line 1 is a header (``"type": "thread"``, format
version, title, timestamps, the producing agent); every later line is one
message, reduced to the portable lowest common denominator — ``role`` +
``content`` (+ ``name``/``timestamp`` when known). Provider plumbing is
deliberately dropped: another tool can read or append such a file with
``json.loads`` per line (or just ``jq``), and a tool that wants to leave
history behind can create the same file with ``{"agent": "<its-name>"}`` in
the header. The directory also holds a ``README.md`` describing the format.
Exports and the README write are best-effort; deletes never prune them.

Foreign threads found in ``.ai/threads/`` (any ``agent`` other than our own)
are adopted into the canonical store on first sight, so history written by
other tools becomes listable/resumable here too.

Nothing is written until a thread actually has content, so non-interactive
one-shots never leave empty files behind.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import config_dir

AGENT_NAME = "goldcomb"


def set_agent_name(name: str | None) -> None:
    """Set the identity stamped on exported thread headers (--agent-name)."""
    global AGENT_NAME
    AGENT_NAME = (name or "").strip() or "goldcomb"


FORMAT = "goldcomb.ai-thread"
#: Pre-rename files remain readable.
_LEGACY_FORMATS = {"nexais.ai-thread"}
FORMAT_VERSION = 1

_PORTABLE_ROLES = {"user", "assistant", "system"}

_FORMAT_README = """\
# AI conversation history

One `<thread-id>.jsonl` file per conversation, in a vendor-neutral interchange
format any AI tool can read or write — one JSON object per line:

- **Line 1 — header**: `{"type": "thread", "format": "goldcomb.ai-thread",
  "version": 1, "id", "title", "created", "updated", "cwd", "agent",
  "provider", "model"}`. `agent` names the tool that wrote the file (e.g.
  `goldcomb`); `provider`/`model` are nullable.
- **Lines 2+ — messages**: `{"role", "content"}` with `role` one of `user`,
  `assistant`, `system`; `name` and `timestamp` are optional.

Rules for writers:

- Append messages chronologically; keep the header's `updated` current.
- Write atomically (temp file + rename) so readers never see a partial file.
- Tool calls/results and other provider-specific plumbing are intentionally
  not representable — keep this to what any tool can consume.
"""


def _drop_empty(messages: list[dict]) -> list[dict]:
    """History repair: remove turns with nothing in them (see from_dict)."""
    return [
        m for m in messages
        if not isinstance(m, dict)
        or (m.get("content") or "").strip()
        or m.get("tool_calls")
        or m.get("role") == "tool"  # empty tool output is still a real result
    ]


def _now() -> str:
    # Microsecond precision so autosaves in the same second still order correctly.
    return datetime.now().isoformat()


def project_key(cwd: Path | None = None) -> str:
    """A filesystem-safe key for the current project directory.

    Mirrors Claude Code's scheme: the absolute path with separators turned into
    dashes, e.g. ``/Users/me/proj`` -> ``-Users-me-proj``.
    """
    try:
        p = (cwd or Path.cwd()).resolve()
    except OSError:
        return "-unknown"
    key = str(p).replace(os.sep, "-")
    return key or "-"


def threads_dir(cwd: Path | None = None) -> Path:
    return config_dir() / "projects" / project_key(cwd) / "threads"


def ai_threads_dir(cwd: Path | None = None) -> Path:
    """The vendor-neutral interchange dir: ``<cwd>/.ai/threads``."""
    return (cwd or Path.cwd()) / ".ai" / "threads"


# Backwards-compatible alias for the previous goldcomb-specific mirror location.
def project_threads_dir(cwd: Path | None = None) -> Path:
    return ai_threads_dir(cwd)


def new_thread_id() -> str:
    """A chronologically sortable, unique id: ``YYYYmmdd-HHMMSS-xxxx``."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def derive_title(messages: list[dict], limit: int = 60) -> str:
    """A short one-line title from the first user message."""
    for m in messages:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            text = " ".join((m["content"] or "").split())
            return text[:limit] + ("…" if len(text) > limit else "")
    return "(empty thread)"


@dataclass
class Thread:
    id: str
    cwd: str
    created: str
    updated: str
    title: str = ""
    provider: str | None = None
    model: str | None = None
    messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "created": self.created,
            "updated": self.updated,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Thread":
        """Build a Thread, repairing history: messages with no content and no
        tool calls (an interrupted or blank model turn) are dropped — every
        provider rejects them on replay, and one poisons the whole thread."""
        return cls(
            id=d["id"],
            cwd=d.get("cwd", ""),
            created=d.get("created", ""),
            updated=d.get("updated", ""),
            title=d.get("title", ""),
            provider=d.get("provider"),
            model=d.get("model"),
            messages=_drop_empty(d.get("messages", [])),
        )

    @property
    def message_count(self) -> int:
        # Count only user/assistant turns — tool results are plumbing.
        return sum(1 for m in self.messages if m.get("role") in ("user", "assistant"))


def new_thread(cwd: Path | None = None, provider: str | None = None,
               model: str | None = None) -> Thread:
    try:
        cwd_str = str((cwd or Path.cwd()).resolve())
    except OSError:
        cwd_str = str(cwd or ".")
    now = _now()
    return Thread(
        id=new_thread_id(), cwd=cwd_str, created=now, updated=now,
        provider=provider, model=model, messages=[],
    )


def save_thread(thread: Thread, cwd: Path | None = None) -> Path:
    """Write a thread atomically, and export it to ``.ai/threads``.

    Refreshes ``updated`` and the derived title. The global JSON copy is
    canonical (full fidelity); the ``.ai/threads`` export is the generic,
    lowest-common-denominator form for other tools, written best-effort.
    """
    d = threads_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    thread.updated = _now()
    if not thread.title or thread.title == "(empty thread)":
        thread.title = derive_title(thread.messages)
    path = d / f"{thread.id}.json"
    _write_atomic(path, json.dumps(thread.to_dict(), indent=2) + "\n")
    _export_to_project(thread, cwd)
    return path


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


# ---- vendor-neutral interchange export (.ai/threads) -------------------------


def _portable_messages(thread: Thread) -> list[dict]:
    """Messages reduced to what any tool can consume: role + content.

    Tool-role messages keep their output as content (it reads naturally);
    tool *calls* attached to assistant turns are flattened to a name list,
    since call ids/argument blobs are provider plumbing.
    """
    out: list[dict] = []
    for m in thread.messages:
        role = m.get("role")
        if role not in _PORTABLE_ROLES and role != "tool":
            continue
        calls = [t.get("name") for t in m.get("tool_calls") or [] if t.get("name")]
        content = m.get("content") or ""
        if not content and calls:
            # Tool-only turns must not flatten to empty content: providers
            # reject empty assistant messages when such a thread is replayed.
            content = "(called tools: " + ", ".join(calls) + ")"
        if not content:
            continue  # nothing portable to say; skip rather than export ""
        line: dict = {"role": role, "content": content}
        if m.get("name"):
            line["name"] = m["name"]
        if calls:
            line["tool_uses"] = calls
        out.append(line)
    return out


def _export_to_project(thread: Thread, cwd: Path | None) -> None:
    """Best-effort export of the thread in the generic interchange format."""
    try:
        proj = Path(thread.cwd) if thread.cwd else (cwd or Path.cwd())
        d = ai_threads_dir(proj)
        d.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "thread",
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "id": thread.id,
            "title": thread.title,
            "created": thread.created,
            "updated": thread.updated,
            "cwd": thread.cwd,
            "agent": AGENT_NAME,
            "agent_version": __version__,
            "provider": thread.provider,
            "model": thread.model,
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        lines += [json.dumps(m, ensure_ascii=False)
                  for m in _portable_messages(thread)]
        _write_atomic(d / f"{thread.id}.jsonl", "\n".join(lines) + "\n")
        _ensure_format_readme(d)
    except OSError:
        pass


def _ensure_format_readme(d: Path) -> None:
    """Drop a format description next to the threads, once."""
    readme = d / "README.md"
    try:
        if not readme.exists():
            readme.write_text(_FORMAT_README)
    except OSError:
        pass


# ---- interchange import (threads written by other tools) ---------------------


def _read_interchange(path: Path) -> Thread | None:
    """Parse a ``.jsonl`` interchange file into a Thread, or None if invalid."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(header, dict) or header.get("type") != "thread":
        return None
    messages: list[dict] = []
    for raw in lines[1:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            continue  # tolerate a torn final line from a non-atomic writer
        if isinstance(m, dict) and m.get("role") in _PORTABLE_ROLES:
            msg = {"role": m["role"], "content": str(m.get("content") or "")}
            if m.get("timestamp"):
                msg["timestamp"] = str(m["timestamp"])
            messages.append(msg)
    return Thread(
        id=str(header.get("id") or path.stem),
        cwd=str(header.get("cwd") or ""),
        created=str(header.get("created") or ""),
        updated=str(header.get("updated") or ""),
        title=str(header.get("title") or ""),
        provider=header.get("provider"),
        model=header.get("model"),
        messages=messages,
    )


def _import_foreign_threads(cwd: Path | None = None) -> None:
    """Adopt threads other tools wrote into ``.ai/threads``.

    A file counts as foreign when its header ``agent`` isn't ours. Imported
    threads keep their id (unless it collides with an existing one) and are
    never re-imported: once adopted, the canonical copy exists and ours is
    newer-or-equal. Best-effort throughout — a bad file is skipped.
    """
    d = ai_threads_dir(cwd)
    try:
        files = list(d.glob("*.jsonl"))
    except OSError:
        return
    store = threads_dir(cwd)
    for f in files:
        try:
            with f.open() as fh:
                header = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(header, dict) or header.get("agent", AGENT_NAME) == AGENT_NAME:
            continue
        t = _read_interchange(f)
        if t is None or not t.messages:
            continue
        if (store / f"{t.id}.json").exists():
            continue  # already imported (or our own thread with that id)
        if not t.cwd:
            try:
                t.cwd = str((cwd or Path.cwd()).resolve())
            except OSError:
                t.cwd = "."
        if not t.title:
            t.title = derive_title(t.messages)
        now = _now()
        t.created = t.created or now
        t.updated = t.updated or now
        try:
            store.mkdir(parents=True, exist_ok=True)
            _write_atomic(store / f"{t.id}.json",
                          json.dumps(t.to_dict(), indent=2) + "\n")
        except OSError:
            continue


# ---- canonical store access ---------------------------------------------------


def list_threads(cwd: Path | None = None) -> list[Thread]:
    """All threads for this project, most-recently-updated first."""
    _import_foreign_threads(cwd)
    d = threads_dir(cwd)
    if not d.is_dir():
        return []
    threads: list[Thread] = []
    for f in d.glob("*.json"):
        try:
            threads.append(Thread.from_dict(json.loads(f.read_text())))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    # Sort by (updated, id) so same-timestamp threads still have a stable order.
    threads.sort(key=lambda t: (t.updated, t.id), reverse=True)
    return threads


def _resolve(cwd: Path | None, ident: str) -> Path | None:
    """Map an exact id or a unique id-prefix to a thread file path."""
    _import_foreign_threads(cwd)
    d = threads_dir(cwd)
    exact = d / f"{ident}.json"
    if exact.exists():
        return exact
    matches = [f for f in d.glob("*.json") if f.stem.startswith(ident)] if d.is_dir() else []
    return matches[0] if len(matches) == 1 else None


def load_thread(ident: str, cwd: Path | None = None) -> Thread | None:
    path = _resolve(cwd, ident)
    if path is None:
        return None
    try:
        return Thread.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def latest_thread(cwd: Path | None = None) -> Thread | None:
    threads = list_threads(cwd)
    return threads[0] if threads else None


def delete_thread(ident: str, cwd: Path | None = None) -> bool:
    path = _resolve(cwd, ident)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
