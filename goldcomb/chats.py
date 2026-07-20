"""Agent-to-agent chat: group discussions and direct messages.

Agents on one project share no transport — each is its own process — but they
do share the project's ``.ai/`` workspace, so chats live there as append-only
JSONL rooms: ``.ai/chats/<id>.jsonl``. Line 1 is a header (id, kind, title,
participants); every later line is a message ``{"ts", "from", "text"}`` or a
meta line (participant joins). Posting is a single small append — safe across
processes without locking.

Delivery is NOT this module's job: the macOS app is the only process connected
to every agent, so it watches these files and wakes addressed agents with the
new messages (the broker). From an agent's point of view: post now, replies
arrive in a later turn. The human user reads every chat in the app and is a
first-class participant ("user") in every group chat.

Identity is the same everywhere: ``scrum.CURRENT_AGENT`` (the agent's human
name), so the chat author, board assignee, thread agent, and memory file all
agree on who spoke.
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import time
from pathlib import Path

CHATS_DIR = Path(".ai") / "chats"

#: Attachments live in a per-room sidecar so the chat file itself stays a
#: stream of small JSON lines. A message references its files by
#: project-relative path (never inline base64, never an absolute user path
#: that would leak into a digest); the bytes sit beside the transcript.
ATTACH_DIR = CHATS_DIR / "attachments"

#: The human owner's handle in chat files; the app renders it as "You" and
#: posts on their behalf. Never an agent name.
USER_HANDLE = "user"

MAX_TEXT = 4000  # a chat message is a message, not a document

#: A chat attachment is a reference, not a payload — but a runaway copy still
#: shouldn't fill the project. Validation-only (raisable later), not a schema
#: constraint.
MAX_ATTACH_BYTES = 25 * 1024 * 1024

_READ_DEFAULT = 30


def _me() -> str:
    from . import scrum
    return scrum.CURRENT_AGENT


def _now() -> float:
    return time.time()


def _slug(text: str, limit: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "chat"


def _resolve_name(name: str) -> str:
    """Chat participants use the same identity rules as everything else:
    human names pass through, functional labels resolve through the roster
    (so '@swift-worker-2' is Quill, not a stranger)."""
    name = " ".join((name or "").split())
    if not name or name.lower() == USER_HANDLE:
        return USER_HANDLE
    from .names import humanize, looks_human
    if looks_human(name.split(" (")[0]):
        return name
    return humanize(name)


# -- storage ----------------------------------------------------------------

def _path(chat_id: str) -> Path:
    return CHATS_DIR / f"{chat_id}.jsonl"


def _append(chat_id: str, obj: dict) -> None:
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_path(chat_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _store_attachment(chat_id: str, source: str) -> dict:
    """Copy one file into the room's sidecar and return its reference record
    ``{"name", "path", "mime", "size"}``. ``path`` is project-relative so it
    means the same thing to the CLI, the app, and the broker digest.

    Raises ValueError if the source is missing or over the size cap — the
    caller must handle it *before* the message line is appended, so a delivered
    reference never dangles (copy-then-append, never the reverse).
    """
    src = Path(source)
    if not src.is_file():
        raise ValueError(f"attachment not found: {source}")
    size = src.stat().st_size
    if size > MAX_ATTACH_BYTES:
        raise ValueError(
            f"attachment too large: {source} is {size} bytes "
            f"(max {MAX_ATTACH_BYTES})"
        )
    dest_dir = ATTACH_DIR / chat_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", src.name).strip("-.") or "file"
    stamp = int(_now() * 1000)
    dest = dest_dir / f"{stamp}-{safe}"
    n = 0
    while dest.exists():  # same-millisecond collisions get a discriminator
        n += 1
        dest = dest_dir / f"{stamp}-{n}-{safe}"
    shutil.copyfile(src, dest)
    return {
        "name": src.name,  # original, for display
        "path": dest.as_posix(),  # project-relative reference (CHATS_DIR is rel)
        "mime": mimetypes.guess_type(src.name)[0] or "application/octet-stream",
        "size": size,
    }


def _store_attachments(chat_id: str, sources: list[str] | None) -> list[dict]:
    return [_store_attachment(chat_id, str(s)) for s in (sources or [])]


def load(chat_id: str) -> tuple[dict, list[dict]] | None:
    """(header, messages) for one chat, meta lines folded into the header's
    participant list. None if the file is missing or unreadable."""
    try:
        lines = _path(chat_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    header: dict | None = None
    messages: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn concurrent append loses one line, not the chat
        if header is None:
            header = obj if obj.get("kind") else {"id": chat_id, "kind": "group",
                                                  "title": chat_id,
                                                  "participants": []}
            if obj.get("kind"):
                continue
        if obj.get("meta") == "add":
            for who in obj.get("who", []):
                if who not in header["participants"]:
                    header["participants"].append(who)
        elif "text" in obj and "from" in obj:
            messages.append(obj)
    if header is None:
        return None
    return header, messages


def list_chats() -> list[tuple[dict, list[dict]]]:
    out = []
    try:
        files = sorted(CHATS_DIR.glob("*.jsonl"))
    except OSError:
        return []
    for f in files:
        loaded = load(f.stem)
        if loaded:
            out.append(loaded)
    out.sort(key=lambda hm: hm[1][-1]["ts"] if hm[1] else 0, reverse=True)
    return out


def _find(chat_id: str) -> tuple[dict, list[dict]] | None:
    """Exact id first, then unique prefix — same affordance as thread ids."""
    if _path(chat_id).exists():
        return load(chat_id)
    hits = [f.stem for f in CHATS_DIR.glob(f"{chat_id}*.jsonl")] \
        if CHATS_DIR.is_dir() else []
    if len(hits) == 1:
        return load(hits[0])
    return None


# -- operations (shared by the tool and the app) ----------------------------

def start(title: str, participants: list[str], kind: str = "group",
          text: str = "", author: str | None = None,
          attachments: list[str] | None = None) -> str:
    """Create a chat and return its id. Group chats always include the user —
    the human is in the room, not looking through a window."""
    author = author or _me()
    names: list[str] = []
    for p in [author, *participants]:
        r = _resolve_name(p)
        if r not in names:
            names.append(r)
    if kind == "group" and USER_HANDLE not in names:
        names.append(USER_HANDLE)
    chat_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}"
    _append(chat_id, {"id": chat_id, "kind": kind, "title": title or chat_id,
                      "participants": names, "created": _now()})
    if text or attachments:
        post(chat_id, text, author=author, attachments=attachments)
    return chat_id


def post(chat_id: str, text: str, author: str | None = None,
         attachments: list[str] | None = None) -> dict:
    author = author or _me()
    found = _find(chat_id)
    if not found:
        raise ValueError(f"no chat matching '{chat_id}'")
    header, _ = found
    real_id = header["id"]
    if author not in header["participants"]:
        _append(real_id, {"meta": "add", "who": [author], "ts": _now()})
    # Copy attachments into the sidecar BEFORE the line lands: a reader that
    # sees the message must always find the files it references. A bad path
    # raises here, so the message is never written at all.
    stored = _store_attachments(real_id, attachments)
    msg: dict = {"ts": _now(), "from": author, "text": text[:MAX_TEXT]}
    if stored:
        msg["attachments"] = stored
    _append(real_id, msg)
    return msg


def dm_chat_id(a: str, b: str) -> str | None:
    """The existing 2-party chat between exactly `a` and `b`, if any."""
    for header, _ in list_chats():
        if header.get("kind") == "dm" and set(header["participants"]) == {a, b}:
            return header["id"]
    return None


def _attach_line(a: dict) -> str:
    """How one attachment reads to an agent. Images get an explicit "you can't
    see this yet" so a text-only model doesn't hallucinate having viewed it
    (true vision is Phase B); everything else points at read_file. The macOS
    broker digest mirrors this wording — keep the two in step."""
    name = a.get("name") or "file"
    if str(a.get("mime", "")).startswith("image/"):
        return f"[image: {name} — you cannot view images yet]"
    return f"[attached: {name} → {a.get('path', '')} — read_file it]"


def render(header: dict, messages: list[dict], last: int = _READ_DEFAULT) -> str:
    who = ", ".join(header["participants"])
    lines = [f"# {header['title']}  [{header['id']}]",
             f"kind: {header.get('kind', 'group')} · participants: {who}"]
    shown = messages[-last:]
    if len(messages) > len(shown):
        lines.append(f"… {len(messages) - len(shown)} earlier messages omitted "
                     "(pass last=N for more)")
    for m in shown:
        t = time.strftime("%m-%d %H:%M", time.localtime(m["ts"]))
        lines.append(f"[{t}] {m['from']}: {m['text']}")
        for a in m.get("attachments", []):
            lines.append(f"           {_attach_line(a)}")
    if not messages:
        lines.append("(no messages yet)")
    return "\n".join(lines)


# -- the agent-facing tool ---------------------------------------------------

def _attach_args(args: dict) -> list[str]:
    """The ``attachments`` argument as a clean list of path strings."""
    raw = args.get("attachments") or []
    if isinstance(raw, str):  # a lone path is a common model mistake — accept it
        raw = [raw]
    return [str(a).strip() for a in raw if str(a).strip()]


def _attach_note(attachments: list[str]) -> str:
    return f" (+{len(attachments)} attachment(s))" if attachments else ""


def chat_tool(args: dict) -> str:
    action = (args.get("action") or "").strip()
    me = _me()

    if action == "list":
        rows = []
        for header, messages in list_chats()[:20]:
            last = messages[-1] if messages else None
            preview = f' — last: {last["from"]}: {last["text"][:60]}' if last else ""
            star = "*" if me in header["participants"] else " "
            rows.append(f"{star} [{header['id']}] {header['title']} "
                        f"({header.get('kind', 'group')}, "
                        f"{len(messages)} msgs){preview}")
        return "\n".join(rows) or "No chats yet. Start one with action='start'."

    if action == "start":
        title = (args.get("title") or "").strip()
        participants = args.get("participants") or []
        if not title:
            return "Error: 'title' is required for start."
        if not isinstance(participants, list) or not participants:
            return "Error: 'participants' (list of agent names) is required."
        try:
            chat_id = start(title, [str(p) for p in participants],
                            text=(args.get("text") or "").strip(),
                            attachments=_attach_args(args))
        except ValueError as e:
            return f"Error: {e}"
        header, _ = load(chat_id)  # type: ignore[misc]
        return (f"Started chat [{chat_id}] '{title}' with "
                f"{', '.join(header['participants'])}. Participants are "
                "notified as messages arrive; replies show up in your later "
                "turns.")

    if action == "dm":
        to = (args.get("to") or "").strip()
        text = (args.get("text") or "").strip()
        attachments = _attach_args(args)
        if not to or (not text and not attachments):
            return "Error: dm needs 'to' (agent name) and 'text' (or 'attachments')."
        other = _resolve_name(to)
        if other == me:
            return "Error: that's you."
        existing = dm_chat_id(me, other)
        try:
            if existing:
                post(existing, text, attachments=attachments)
                return f"Sent to {other} in [{existing}]{_attach_note(attachments)}."
            chat_id = start(f"{me} ↔ {other}", [other], kind="dm", text=text,
                            attachments=attachments)
        except ValueError as e:
            return f"Error: {e}"
        return (f"Started DM [{chat_id}] with {other} and sent your message"
                f"{_attach_note(attachments)}.")

    if action == "post":
        chat_id = (args.get("id") or "").strip()
        text = (args.get("text") or "").strip()
        attachments = _attach_args(args)
        if not chat_id or (not text and not attachments):
            return "Error: post needs 'id' and 'text' (or 'attachments')."
        try:
            post(chat_id, text, attachments=attachments)
        except ValueError as e:
            return f"Error: {e}"
        return f"Posted to [{chat_id}]{_attach_note(attachments)}."

    if action == "read":
        chat_id = (args.get("id") or "").strip()
        if not chat_id:
            return "Error: read needs 'id'."
        found = _find(chat_id)
        if not found:
            return f"Error: no chat matching '{chat_id}'."
        return render(*found, last=int(args.get("last") or _READ_DEFAULT))

    if action == "add":
        chat_id = (args.get("id") or "").strip()
        participants = args.get("participants") or []
        if not chat_id or not participants:
            return "Error: add needs 'id' and 'participants'."
        found = _find(chat_id)
        if not found:
            return f"Error: no chat matching '{chat_id}'."
        header, _ = found
        new = [_resolve_name(str(p)) for p in participants]
        new = [n for n in new if n not in header["participants"]]
        if new:
            _append(header["id"], {"meta": "add", "who": new, "ts": _now()})
        return f"Participants now: {', '.join(header['participants'] + new)}."

    return f"Unknown action '{action}'."
