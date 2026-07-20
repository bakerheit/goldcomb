"""A JIRA-style scrum board for agents, persisted as JSON in the project dir.

Every item — epic, story, task — is a *ticket* with an id like ``DEMO-7``:
one sequence per board under a project key (set or derived at init). Tickets
carry an ``assignee``; moving a task to ``in_progress`` auto-assigns it to the
current agent identity (see :func:`set_agent`), so the board always shows
which agents are working on which tickets.

The board lives at ``<project>/.ai/scrum/board.json`` — inside the project's
vendor-neutral ``.ai`` workspace folder, alongside ``.ai/threads/`` (see
goldcomb.threads), so any AI tool or GUI can read and render it. A dict of
epics -> stories -> tasks plus an optional sprint, mutated through one entry
point, :func:`scrum`. Boards written by older versions to
``.goldcomb/board.json`` are migrated on first load (the old file is kept as
``board.json.migrated``).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

STATUSES = ("todo", "in_progress", "blocked", "done")
PRIORITIES = ("low", "medium", "high")

ACTIONS = (
    "init", "show", "burndown",
    "epic_add", "epic_list", "epic_del",
    "story_add", "story_list", "story_show", "story_update", "story_del",
    "task_add", "task_update", "task_list", "task_del", "ticket_add",
    "assign", "comment", "find", "ticket_show", "history",
    "sprint_start", "sprint_add", "sprint_remove", "sprint_status", "sprint_end",
)

#: Mutations recorded in the board's audit log (board["history"], a capped
#: ring). Read actions never log.
_HISTORY_CAP = 200

#: The agent identity stamped as default assignee on tickets. The CLI sets it
#: per session (--agent-name), and sub-agent runs swap in their label for the
#: duration of their tool calls.
CURRENT_AGENT = "goldcomb"


def set_agent(name: str | None) -> None:
    global CURRENT_AGENT
    CURRENT_AGENT = (name or "").strip() or "goldcomb"


# Status moves we allow; anything else is refused with a hint. Keeps the board
# honest: no un-blocking straight to done, no done work jumping mid-flight.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "todo": {"in_progress", "blocked", "done"},
    "in_progress": {"todo", "blocked", "done"},
    "blocked": {"todo", "in_progress"},
    "done": {"todo"},
}

_ROOT = Path(".ai") / "scrum"
_BOARD = _ROOT / "board.json"
_LEGACY_BOARD = Path(".nexais") / "board.json"  # pre-rename era

#: Liveness sidecar: agent label -> last-heartbeat unix time. Kept OUT of
#: board.json on purpose — workers beat every few seconds, and the board file
#: is history-logged, atomic-rewritten, and polled by GUIs; a beat must cost
#: one tiny file write and zero board churn.
_HEARTBEATS = _ROOT / "heartbeats.json"
_HEARTBEAT_MAX_AGE_S = 24 * 3600.0

_SCRUM_README = """\
# AI scrum board

`board.json` is a lightweight, vendor-neutral scrum board any AI tool can read
or write: `{"meta": {"project", "created"}, "epics": {id: {title, stories:
[story-id]}}, "stories": {id: {title, priority, notes, tasks: [{id, title,
status, points, notes}]}}, "sprint": {"number", "goal", "active", "stories"}
| null, "counters": {...}}`. Task `status` is one of `todo`, `in_progress`,
`blocked`, `done`. Write the whole file atomically.
"""


def heartbeat(agent: str | None = None) -> None:
    """Record that ``agent`` (default: the current identity) is alive now.

    Sub-agent runs beat periodically (agents.SubAgentHandle._beat) so a
    stale-assignee audit can tell a quietly-working agent from a dead one.
    No-op when the project has no board; never raises — liveness must never
    kill a run.
    """
    try:
        if not _BOARD.exists():
            return
        who = (agent or CURRENT_AGENT or "").strip()
        if not who:
            return
        now = time.time()
        beats = agent_heartbeats()
        beats[who] = now
        cutoff = now - _HEARTBEAT_MAX_AGE_S
        beats = {k: v for k, v in beats.items() if v >= cutoff}
        # Per-pid tmp: concurrent worker processes must not clobber each
        # other's half-written file; the final replace is atomic either way.
        tmp = _HEARTBEATS.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(beats, indent=2) + "\n")
        tmp.replace(_HEARTBEATS)
    except OSError:
        pass


def agent_heartbeats() -> dict[str, float]:
    """Agent label -> last-beat unix time from the liveness sidecar ({} when
    absent/corrupt). The reader half of :func:`heartbeat`."""
    try:
        data = json.loads(_HEARTBEATS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(v, (int, float))}


def derive_key(project: str) -> str:
    """A JIRA-style project key from the project name: 'demo app' -> 'DA',
    'goldcomb' -> 'NEXA'."""
    words = re.findall(r"[A-Za-z0-9]+", project or "")
    if not words:
        return "TCK"
    if len(words) == 1:
        return words[0][:4].upper()
    return "".join(w[0] for w in words)[:5].upper()


def new_board(project: str, key: str | None = None) -> dict[str, Any]:
    return {
        "meta": {
            "version": 2,
            "project": project,
            "key": (key or derive_key(project)).upper(),
            "created": time.time(),
        },
        "epics": {},
        "stories": {},
        "sprint": None,
        "counters": {"ticket": 0},
        "history": [],
    }


def is_enabled() -> bool:
    """Whether scrum tracking is on for this project (cwd).

    Opt-in: only projects with a board (created via /scrum on or the GUI)
    offer the scrum tool to models, and a board can be switched off without
    losing data (meta.enabled=false).
    """
    board = _read_board(_BOARD) or _read_board(_LEGACY_BOARD)
    return board is not None and board["meta"].get("enabled", True)


def enable(project: str | None = None, key: str | None = None) -> str:
    """Turn tracking on: create the board if needed, or re-enable a paused one."""
    board = load_board()  # migrates any legacy board first
    created = board is None
    if created:
        name = (project or "").strip() or _cwd_name()
        board = new_board(name, key)
    board["meta"]["enabled"] = True
    try:
        save_board(board)
    except OSError as e:
        return f"Error enabling scrum: {e}"
    what = "new board" if created else "existing board"
    return f"Scrum tracking on ({what} [{board['meta'].get('key', '?')}] at {_BOARD})"


def disable() -> str:
    """Turn tracking off. The board file is kept — nothing is deleted."""
    board = load_board()
    if board is None:
        return "Scrum is already off here (no board)."
    board["meta"]["enabled"] = False
    try:
        save_board(board)
    except OSError as e:
        return f"Error disabling scrum: {e}"
    return "Scrum tracking off (board kept; /scrum on re-enables it)."


def _cwd_name() -> str:
    try:
        return Path.cwd().name
    except OSError:
        return ""


def _read_board(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "stories" not in data:
        return None
    # Older files may predate later fields; fill gaps rather than reject.
    base = new_board(data.get("meta", {}).get("project", ""))
    base.update({k: v for k, v in data.items() if k in base})
    return base


def load_board() -> dict[str, Any] | None:
    board = _read_board(_BOARD)
    if board is not None:
        return board
    legacy = _read_board(_LEGACY_BOARD)
    if legacy is not None:
        # One-time migration from the pre-.ai location; nothing is destroyed —
        # the old file stays behind as a .migrated backup.
        try:
            save_board(legacy)
            _LEGACY_BOARD.replace(_LEGACY_BOARD.with_suffix(".json.migrated"))
        except OSError:
            pass
        return legacy
    return None


def save_board(board: dict[str, Any]) -> None:
    _ROOT.mkdir(parents=True, exist_ok=True)
    readme = _ROOT / "README.md"
    if not readme.exists():
        try:
            readme.write_text(_SCRUM_README)
        except OSError:  # the board itself is what matters
            pass
    tmp = _BOARD.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(board, indent=2) + "\n")
    tmp.replace(_BOARD)


def _next_ticket(board: dict[str, Any]) -> str:
    """The next JIRA-style ticket id, e.g. DEMO-7. One sequence for the whole
    board — epics, stories, and tasks share it, like JIRA issues do. Boards
    from the pre-ticket era (E1-x/S1-x ids) keep their old ids; new items get
    numbered tickets under a key derived on first use."""
    meta = board.setdefault("meta", {})
    key = meta.get("key") or derive_key(meta.get("project", ""))
    meta["key"] = key
    counters = board.setdefault("counters", {})
    counters["ticket"] = counters.get("ticket", 0) + 1
    return f"{key}-{counters['ticket']}"


def _find_task(board, task_id):
    for story in board["stories"].values():
        for task in story.get("tasks", []):
            if task.get("id") == task_id:
                return story, task
    return None, None


def _require_story(board, args):
    sid = str(args.get("story") or "").strip()
    if not sid:
        return "", None, "Error: this action requires story=<id>."
    story = board["stories"].get(sid)
    if story is None:
        return sid, None, f"Error: no story {sid}."
    return sid, story, None


def _priority(value) -> str:
    v = str(value or "medium").strip().lower()
    return v if v in PRIORITIES else "medium"


def _labels(value) -> list[str]:
    """Normalize a labels argument: comma string or list -> clean list."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return []
    out = []
    for p in parts:
        p = p.strip().lower()
        if p and p not in out:
            out.append(p)
    return out


def _ids(value) -> list[str]:
    """Normalize a ticket-id list argument (comma string or list)."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return []
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _find_any(board, ticket_id):
    """(kind, container, item) for any ticket id: epic, story, or task."""
    if ticket_id in board["epics"]:
        return "epic", None, board["epics"][ticket_id]
    if ticket_id in board["stories"]:
        return "story", None, board["stories"][ticket_id]
    story, task = _find_task(board, ticket_id)
    if task is not None:
        return "task", story, task
    return None, None, None


def _log(board, action: str, summary: str) -> None:
    history = board.setdefault("history", [])
    history.append({
        "at": time.time(), "agent": CURRENT_AGENT,
        "action": action, "summary": summary,
    })
    del history[:-_HISTORY_CAP]


def _open_blockers(board, task) -> list[str]:
    """Ids in this task's blocked_by list that are not done yet."""
    out = []
    for bid in task.get("blocked_by", []):
        kind, _c, item = _find_any(board, bid)
        if kind == "task" and item.get("status") != "done":
            out.append(bid)
        elif kind == "story" and any(
            tk.get("status") != "done" for tk in item.get("tasks", [])
        ):
            out.append(bid)
    return out


def _points(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _story_points(story) -> tuple[int, int]:
    """(done_points, total_points) for a story, derived from its tasks."""
    done = total = 0
    for task in story.get("tasks", []):
        pts = int(task.get("points", 0) or 0)
        total += pts
        if task.get("status") == "done":
            done += pts
    return done, total


# --------------------------------------------------------------------------
# formatting helpers (plain text; the CLI prints tool output as-is)
# --------------------------------------------------------------------------

def _who(item) -> str:
    return f" @{item['assignee']}" if item.get("assignee") else ""


def _fmt_story_line(sid, story) -> str:
    done_pts, total_pts = _story_points(story)
    n_tasks = len(story.get("tasks", []))
    n_done = sum(1 for t in story.get("tasks", []) if t.get("status") == "done")
    pts = f" {done_pts}/{total_pts}pt" if total_pts else ""
    tasks = f" [{n_done}/{n_tasks} tasks]" if n_tasks else ""
    return f"{sid}  {story['title']}{pts}{tasks}{_who(story)}"


def _in_progress_tasks(board):
    """(task, story_id) pairs for everything currently being worked on."""
    return [
        (task, sid)
        for sid, story in board["stories"].items()
        for task in story.get("tasks", [])
        if task.get("status") == "in_progress"
    ]


def _fmt_board(board) -> str:
    key = board["meta"].get("key")
    name = board["meta"].get("project") or "(unnamed)"
    tag = f" [{key}]" if key else ""
    lines = [f"Board: {name}{tag}  ({_BOARD})"]
    sprint = board.get("sprint")
    if sprint:
        state = "active" if sprint.get("active") else "ended"
        lines.append(f"Sprint {sprint['number']} ({state}): {sprint.get('goal', '')}")
    active = _in_progress_tasks(board)
    if active:
        lines.append("In progress:")
        for task, sid in active:
            who = _who(task) or " (unassigned)"
            lines.append(f"  ~ {task['id']}  {task['title']}{who}  ({sid})")
    if not board["epics"] and not board["stories"]:
        lines.append("(empty - add an epic with epic_add, or a story with story_add)")
        return "\n".join(lines)
    for eid, epic in board["epics"].items():
        lines.append(f"\n{eid}  {epic['title']}")
        for sid in epic.get("stories", []):
            story = board["stories"].get(sid)
            if story:
                lines.append("  " + _fmt_story_line(sid, story))
    assigned = {sid for e in board["epics"].values() for sid in e.get("stories", [])}
    unassigned = [sid for sid in board["stories"] if sid not in assigned]
    if unassigned:
        lines.append("\n(no epic)")
        for sid in unassigned:
            lines.append("  " + _fmt_story_line(sid, board["stories"][sid]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# actions - each returns (board, message, changed); scrum() saves when changed
# --------------------------------------------------------------------------

def _a_init(board, args):
    project = str(args.get("project") or "").strip() or board["meta"].get("project") or ""
    board["meta"]["project"] = project
    key = str(args.get("key") or "").strip()
    if key:
        board["meta"]["key"] = re.sub(r"[^A-Za-z0-9]", "", key).upper()[:6] or "TCK"
    elif not board["meta"].get("key"):
        board["meta"]["key"] = derive_key(project)
    return board, (
        f"Initialized board for {project or 'this project'} "
        f"[{board['meta']['key']}] at {_BOARD}"
    ), True


def _a_show(board, args):
    return board, _fmt_board(board), False


def _a_epic_add(board, args):
    title = str(args.get("title") or "").strip()
    if not title:
        return board, "Error: epic_add requires a title.", False
    eid = _next_ticket(board)
    board["epics"][eid] = {"title": title, "stories": [], "created": time.time()}
    return board, f"Added epic {eid}: {title}", True


def _a_epic_list(board, args):
    if not board["epics"]:
        return board, "(no epics)", False
    lines = [
        f"{eid}  {epic['title']}  ({len(epic.get('stories', []))} stories)"
        for eid, epic in board["epics"].items()
    ]
    return board, "\n".join(lines), False


def _a_story_add(board, args):
    title = str(args.get("title") or "").strip()
    if not title:
        return board, "Error: story_add requires a title.", False
    sid = _next_ticket(board)
    board["stories"][sid] = {
        "title": title,
        "priority": _priority(args.get("priority")),
        "notes": str(args.get("notes") or ""),
        "assignee": str(args.get("assignee") or "").strip(),
        "tasks": [],
        "created": time.time(),
    }
    eid = str(args.get("epic") or "").strip()
    if eid:
        epic = board["epics"].get(eid)
        if epic is None:
            return board, f"Error: no epic {eid} (story not added).", False
        epic.setdefault("stories", []).append(sid)
        return board, f"Added story {sid} to {eid}: {title}", True
    return board, f"Added story {sid}: {title} (no epic - assign one with story_update)", True


def _a_story_list(board, args):
    eid = str(args.get("epic") or "").strip()
    if eid and eid not in board["epics"]:
        return board, f"Error: no epic {eid}.", False
    sids = board["epics"][eid].get("stories", []) if eid else list(board["stories"].keys())
    if not sids:
        return board, "(no stories)", False
    return board, "\n".join(
        _fmt_story_line(sid, board["stories"][sid]) for sid in sids if sid in board["stories"]
    ), False


def _a_story_show(board, args):
    sid, story, err = _require_story(board, args)
    if err:
        return board, err, False
    done_pts, total_pts = _story_points(story)
    lines = [
        f"{sid}  {story['title']}",
        f"priority: {story.get('priority', 'medium')}   points: {done_pts}/{total_pts}",
    ]
    if story.get("notes"):
        lines.append(f"notes: {story['notes']}")
    if story.get("tasks"):
        lines.append("tasks:")
        for task in story["tasks"]:
            mark = {"done": "x", "in_progress": "~", "blocked": "!"}.get(task.get("status"), " ")
            pts = f" ({task['points']}pt)" if task.get("points") else ""
            lines.append(f"  [{mark}] {task['id']}  {task['title']}{pts}{_who(task)}")
    else:
        lines.append("(no tasks - break it down with task_add)")
    return board, "\n".join(lines), False


def _a_story_update(board, args):
    sid, story, err = _require_story(board, args)
    if err:
        return board, err, False
    changed = []
    if args.get("title"):
        story["title"] = str(args["title"]).strip()
        changed.append("title")
    if args.get("priority"):
        story["priority"] = _priority(args["priority"])
        changed.append("priority")
    if args.get("notes") is not None:
        story["notes"] = str(args["notes"])
        changed.append("notes")
    if args.get("labels") is not None:
        story["labels"] = _labels(args["labels"])
        changed.append(f"labels->{','.join(story['labels']) or '(none)'}")
    if args.get("epic"):
        eid = str(args["epic"]).strip()
        if eid not in board["epics"]:
            return board, f"Error: no epic {eid}.", False
        for epic in board["epics"].values():
            if sid in epic.get("stories", []):
                epic["stories"].remove(sid)
        board["epics"][eid].setdefault("stories", []).append(sid)
        changed.append(f"epic->{eid}")
    if not changed:
        return board, ("Nothing to update (pass title, priority, notes, labels, "
                       "or epic)."), False
    return board, f"Updated {sid} ({', '.join(changed)})", True


def _a_task_add(board, args):
    sid, story, err = _require_story(board, args)
    if err:
        return board, err, False
    title = str(args.get("title") or "").strip()
    if not title:
        return board, "Error: task_add requires a title.", False
    tid = _next_ticket(board)
    story.setdefault("tasks", []).append({
        "id": tid,
        "title": title,
        "status": "todo",
        "points": _points(args.get("points")),
        "assignee": str(args.get("assignee") or "").strip(),
        "notes": str(args.get("notes") or ""),
        "created": time.time(),
    })
    return board, f"Added task {tid} to {sid}: {title}", True


def _a_task_update(board, args):
    tid = str(args.get("task") or "").strip()
    if not tid:
        return board, "Error: task_update requires task=<id>.", False
    _story, task = _find_task(board, tid)
    if task is None:
        return board, f"Error: no task {tid}.", False
    changed = []
    new_status = args.get("status")
    if new_status:
        new_status = str(new_status).strip().lower()
        if new_status not in STATUSES:
            return board, f"Error: status must be one of {', '.join(STATUSES)}.", False
        cur = task.get("status", "todo")
        if new_status != cur:
            if new_status not in _VALID_TRANSITIONS.get(cur, set()):
                allowed = ", ".join(sorted(_VALID_TRANSITIONS.get(cur, ()))) or "none"
                return board, (f"Refused: {cur} -> {new_status} is not a valid "
                               f"transition (allowed: {allowed})."), False
            if new_status == "done":
                open_blockers = _open_blockers(board, task)
                if open_blockers:
                    return board, (
                        f"Refused: {tid} is blocked by open ticket(s) "
                        f"{', '.join(open_blockers)} — finish those first "
                        "(or clear blocked_by)."), False
            task["status"] = new_status
            task[f"{new_status}_at"] = time.time()
            changed.append(f"status->{new_status}")
            # JIRA-style: starting progress claims the ticket for whoever is
            # working, so the board always shows who is on what.
            if new_status == "in_progress" and not task.get("assignee"):
                task["assignee"] = CURRENT_AGENT
                changed.append(f"assignee->{CURRENT_AGENT}")
    if args.get("assignee") is not None:
        task["assignee"] = str(args["assignee"]).strip()
        changed.append(f"assignee->{task['assignee'] or '(none)'}")
    if args.get("title"):
        task["title"] = str(args["title"]).strip()
        changed.append("title")
    if args.get("points") is not None:
        task["points"] = _points(args["points"])
        changed.append("points")
    if args.get("notes") is not None:
        task["notes"] = str(args["notes"])
        changed.append("notes")
    if args.get("labels") is not None:
        task["labels"] = _labels(args["labels"])
        changed.append(f"labels->{','.join(task['labels']) or '(none)'}")
    if args.get("blocked_by") is not None:
        deps = _ids(args["blocked_by"])
        unknown = [d for d in deps if _find_any(board, d)[0] is None]
        if unknown:
            return board, f"Error: unknown ticket(s) in blocked_by: {', '.join(unknown)}.", False
        if tid in deps:
            return board, f"Error: {tid} cannot block itself.", False
        task["blocked_by"] = deps
        changed.append(f"blocked_by->{','.join(deps) or '(none)'}")
    if not changed:
        return board, ("Nothing to update (pass status, title, points, notes, "
                       "labels, or blocked_by)."), False
    return board, f"Updated {tid} ({', '.join(changed)})", True


def _a_task_list(board, args):
    want = str(args.get("status") or "").strip().lower()
    if want and want not in STATUSES:
        return board, f"Error: status must be one of {', '.join(STATUSES)}.", False
    want_label = str(args.get("label") or "").strip().lower()
    want_who = str(args.get("assignee") or "").strip()
    rows = []
    for sid, story in board["stories"].items():
        for task in story.get("tasks", []):
            if want and task.get("status") != want:
                continue
            if want_label and want_label not in task.get("labels", []):
                continue
            if want_who and task.get("assignee") != want_who:
                continue
            mark = {"done": "x", "in_progress": "~", "blocked": "!"}.get(task.get("status"), " ")
            tags = "".join(f" #{lb}" for lb in task.get("labels", []))
            dep = f" ⛔{','.join(task['blocked_by'])}" if task.get("blocked_by") else ""
            rows.append(f"[{mark}] {task['id']}  ({sid}) {task['title']}{tags}{dep}{_who(task)}")
    return board, ("\n".join(rows) if rows else "(no matching tasks)"), False


def _a_ticket_add(board, args):
    """File a complete ticket in one call: a story plus its first task (same
    title), under an epic — created on the fly when none is given and none
    exist. Optionally drops it straight into the active sprint. This is what
    GUIs should call: no multi-step id round-trips."""
    title = str(args.get("title") or "").strip()
    if not title:
        return board, "Error: ticket_add requires a title.", False

    eid = str(args.get("epic") or "").strip()
    made_epic = False
    if eid:
        if eid not in board["epics"]:
            return board, f"Error: no epic {eid}.", False
    elif board["epics"]:
        eid = next(iter(board["epics"]))
    else:
        eid = _next_ticket(board)
        board["epics"][eid] = {"title": "Backlog", "stories": [],
                               "created": time.time()}
        made_epic = True

    sid = _next_ticket(board)
    board["stories"][sid] = {
        "title": title,
        "priority": _priority(args.get("priority")),
        "notes": str(args.get("notes") or ""),
        "assignee": "",
        "labels": _labels(args.get("labels")),
        "tasks": [],
        "created": time.time(),
    }
    board["epics"][eid].setdefault("stories", []).append(sid)

    tid = _next_ticket(board)
    board["stories"][sid]["tasks"].append({
        "id": tid,
        "title": title,
        "status": "todo",
        "points": _points(args.get("points")),
        "assignee": str(args.get("assignee") or "").strip(),
        "labels": _labels(args.get("labels")),
        "notes": "",
        "created": time.time(),
    })

    bits = [f"Filed {tid} ({sid}, epic {eid}"
            + (" — created" if made_epic else "") + f"): {title}"]
    if args.get("sprint"):
        sprint = board.get("sprint")
        if sprint and sprint.get("active"):
            sprint.setdefault("stories", []).append(sid)
            bits.append(f"added to sprint {sprint['number']}")
        else:
            bits.append("no active sprint to add to")
    return board, "; ".join(bits), True


def _a_task_del(board, args):
    tid = str(args.get("task") or "").strip()
    if not tid:
        return board, "Error: task_del requires task=<id>.", False
    for story in board["stories"].values():
        tasks = story.get("tasks", [])
        for i, task in enumerate(tasks):
            if task.get("id") == tid:
                tasks.pop(i)
                return board, f"Deleted task {tid}: {task.get('title', '')}", True
    return board, f"Error: no task {tid}.", False


def _a_story_del(board, args):
    sid, story, err = _require_story(board, args)
    if err:
        return board, err, False
    tasks = story.get("tasks", [])
    if tasks and not args.get("force"):
        return board, (f"Refused: {sid} has {len(tasks)} task(s) "
                       f"({', '.join(t['id'] for t in tasks)}). Pass force=true "
                       "to delete the story and its tasks."), False
    del board["stories"][sid]
    for epic in board["epics"].values():
        if sid in epic.get("stories", []):
            epic["stories"].remove(sid)
    sprint = board.get("sprint")
    if sprint and sid in sprint.get("stories", []):
        sprint["stories"].remove(sid)
    return board, f"Deleted story {sid}: {story.get('title', '')}", True


def _a_epic_del(board, args):
    eid = str(args.get("epic") or "").strip()
    if not eid:
        return board, "Error: epic_del requires epic=<id>.", False
    epic = board["epics"].get(eid)
    if epic is None:
        return board, f"Error: no epic {eid}.", False
    stories = epic.get("stories", [])
    if stories and not args.get("force"):
        return board, (f"Refused: {eid} has {len(stories)} story(ies). Pass "
                       "force=true to delete the epic (its stories are kept, "
                       "just un-grouped)."), False
    del board["epics"][eid]
    return board, (f"Deleted epic {eid}: {epic.get('title', '')}"
                   + (f" ({len(stories)} stories un-grouped)" if stories else "")), True


def _a_comment(board, args):
    tid = str(args.get("ticket") or args.get("task") or args.get("story") or "").strip()
    if not tid:
        return board, "Error: comment requires ticket=<id>.", False
    text = str(args.get("text") or "").strip()
    if not text:
        return board, "Error: comment requires text=<...>.", False
    kind, _c, item = _find_any(board, tid)
    if kind is None:
        return board, f"Error: no ticket {tid}.", False
    item.setdefault("comments", []).append({
        "who": CURRENT_AGENT, "text": text, "at": time.time(),
    })
    return board, f"Commented on {tid} ({len(item['comments'])} total)", True


def _fmt_comments(item, limit=5) -> list[str]:
    comments = item.get("comments", [])
    lines = []
    if comments:
        lines.append(f"comments ({len(comments)}):")
        for c in comments[-limit:]:
            when = time.strftime("%m-%d %H:%M", time.localtime(c.get("at", 0)))
            lines.append(f"  [{when}] @{c.get('who', '?')}: {c.get('text', '')}")
    return lines


def _a_find(board, args):
    query = str(args.get("query") or args.get("text") or "").strip().lower()
    if not query:
        return board, "Error: find requires query=<text>.", False

    def hit(*fields):
        return any(query in str(f or "").lower() for f in fields)

    rows = []
    for eid, epic in board["epics"].items():
        if hit(eid, epic.get("title")):
            rows.append(f"epic   {eid}  {epic['title']}")
    for sid, story in board["stories"].items():
        if hit(sid, story.get("title"), story.get("notes"),
               " ".join(story.get("labels", []))):
            rows.append(f"story  {_fmt_story_line(sid, story)}")
        for task in story.get("tasks", []):
            if hit(task.get("id"), task.get("title"), task.get("notes"),
                   task.get("assignee"), " ".join(task.get("labels", []))):
                mark = {"done": "x", "in_progress": "~", "blocked": "!"}.get(
                    task.get("status"), " ")
                rows.append(f"task   [{mark}] {task['id']}  ({sid}) "
                            f"{task['title']}{_who(task)}")
    return board, ("\n".join(rows) if rows else f"(nothing matches {query!r})"), False


def _a_ticket_show(board, args):
    tid = str(args.get("ticket") or args.get("task") or args.get("story")
              or args.get("epic") or "").strip()
    if not tid:
        return board, "Error: ticket_show requires ticket=<id>.", False
    kind, container, item = _find_any(board, tid)
    if kind is None:
        return board, f"Error: no ticket {tid}.", False
    if kind == "epic":
        lines = [f"epic {tid}  {item['title']}",
                 f"stories: {', '.join(item.get('stories', [])) or '(none)'}"]
        lines += _fmt_comments(item)
        return board, "\n".join(lines), False
    if kind == "story":
        board, msg, _ = _a_story_show(board, {"story": tid})
        extra = _fmt_comments(item)
        return board, msg + ("\n" + "\n".join(extra) if extra else ""), False
    # task
    sid = next((s for s, st in board["stories"].items() if item in st.get("tasks", [])), "?")
    lines = [
        f"task {tid}  {item['title']}  ({sid})",
        f"status: {item.get('status', 'todo')}   points: {item.get('points', 0)}"
        f"   assignee: {item.get('assignee') or '(none)'}",
    ]
    if item.get("labels"):
        lines.append("labels: " + ", ".join(item["labels"]))
    if item.get("blocked_by"):
        open_b = _open_blockers(board, item)
        lines.append("blocked_by: " + ", ".join(
            f"{b}{'' if b in open_b else ' (done)'}" for b in item["blocked_by"]))
    if item.get("notes"):
        lines.append(f"notes: {item['notes']}")
    lines += _fmt_comments(item)
    return board, "\n".join(lines), False


def _a_history(board, args):
    history = board.get("history", [])
    if not history:
        return board, "(no history yet)", False
    try:
        limit = max(1, min(int(args.get("limit") or 20), _HISTORY_CAP))
    except (TypeError, ValueError):
        limit = 20
    lines = []
    for h in history[-limit:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(h.get("at", 0)))
        lines.append(f"[{when}] @{h.get('agent', '?')}  {h.get('action', '?')}: "
                     f"{h.get('summary', '')}")
    return board, "\n".join(lines), False


def _a_assign(board, args):
    tid = str(args.get("ticket") or args.get("task") or args.get("story") or "").strip()
    if not tid:
        return board, "Error: assign requires ticket=<id>.", False
    explicit = str(args.get("assignee") or "").strip()
    assignee = explicit or CURRENT_AGENT
    # Explicit labels resolve through the deploy roster: assigning to
    # "swift-worker-2" assigns to the person that label is (or will be)
    # deployed as, so board identity and worker identity never diverge. The
    # no-arg default (the current agent claiming a ticket) stays verbatim,
    # matching the in_progress auto-assign path.
    if explicit:
        from .names import humanize, looks_human
        if not looks_human(explicit.split(" (")[0]):
            assignee = humanize(explicit)
    _story, task = _find_task(board, tid)
    if task is not None:
        task["assignee"] = assignee
        return board, f"Assigned {tid} to {assignee}", True
    story = board["stories"].get(tid)
    if story is not None:
        story["assignee"] = assignee
        return board, f"Assigned {tid} to {assignee}", True
    return board, f"Error: no ticket {tid}.", False


def _a_sprint_start(board, args):
    if board.get("sprint") and board["sprint"].get("active"):
        return board, (f"Error: sprint {board['sprint']['number']} is already active - "
                       "sprint_end it first."), False
    number = (board.get("sprint") or {}).get("number", 0) + 1
    goal = str(args.get("goal") or "").strip()
    board["sprint"] = {
        "number": number, "goal": goal, "active": True,
        "stories": [], "started": time.time(),
    }
    return board, (f"Started sprint {number}: {goal or '(no goal)'} - "
                   "add stories with sprint_add"), True


def _a_sprint_add(board, args):
    sprint = board.get("sprint")
    if not sprint or not sprint.get("active"):
        return board, "Error: no active sprint - sprint_start first.", False
    sid, _story, err = _require_story(board, args)
    if err:
        return board, err, False
    if sid in sprint.setdefault("stories", []):
        return board, f"{sid} is already in sprint {sprint['number']}.", False
    sprint["stories"].append(sid)
    return board, f"Added {sid} to sprint {sprint['number']}", True


def _a_sprint_remove(board, args):
    sprint = board.get("sprint")
    if not sprint or not sprint.get("active"):
        return board, "Error: no active sprint.", False
    sid, _story, err = _require_story(board, args)
    if err:
        return board, err, False
    if sid not in sprint.get("stories", []):
        return board, f"{sid} is not in sprint {sprint['number']}.", False
    sprint["stories"].remove(sid)
    return board, f"Removed {sid} from sprint {sprint['number']}", True


def _sprint_progress(board, sprint) -> tuple[int, int, int]:
    """(done_points, total_points, open_tasks) across the sprint's stories."""
    total = done = open_tasks = 0
    for sid in sprint.get("stories", []):
        story = board["stories"].get(sid)
        if not story:
            continue
        d, t = _story_points(story)
        total += t
        done += d
        open_tasks += sum(1 for tk in story.get("tasks", []) if tk.get("status") != "done")
    return done, total, open_tasks


def _a_sprint_status(board, args):
    sprint = board.get("sprint")
    if not sprint:
        return board, "(no sprint - sprint_start to begin one)", False
    state = "active" if sprint.get("active") else "ended"
    lines = [f"Sprint {sprint['number']} ({state}): {sprint.get('goal', '')}"]
    for sid in sprint.get("stories", []):
        story = board["stories"].get(sid)
        if story:
            lines.append("  " + _fmt_story_line(sid, story))
    done, total, open_tasks = _sprint_progress(board, sprint)
    if total:
        lines.append(f"Progress: {done}/{total} points done")
    lines.append(f"Open tasks: {open_tasks}")
    return board, "\n".join(lines), False


def _a_sprint_end(board, args):
    sprint = board.get("sprint")
    if not sprint:
        return board, "Error: no sprint to end.", False
    if not sprint.get("active"):
        return board, f"Sprint {sprint['number']} already ended.", False
    sprint["active"] = False
    sprint["ended"] = time.time()
    done, total, open_tasks = _sprint_progress(board, sprint)
    summary = (f"Sprint {sprint['number']} ended: {done}/{total} points done, "
               f"{open_tasks} open tasks.")
    if open_tasks:
        leftover = [t["id"] for sid in sprint.get("stories", [])
                    for t in board["stories"].get(sid, {}).get("tasks", [])
                    if t.get("status") != "done"]
        summary += f" Carry over: {', '.join(leftover)}."
    return board, summary, True


def _a_burndown(board, args):
    sprint = board.get("sprint")
    if not sprint:
        return board, "(no sprint)", False
    done, total, open_tasks = _sprint_progress(board, sprint)
    return board, (
        f"Sprint {sprint['number']}: {done}/{total} points done, "
        f"{total - done} remaining, {open_tasks} open tasks."
    ), False


_ACTION_FUNCS = {
    "init": _a_init,
    "show": _a_show,
    "burndown": _a_burndown,
    "epic_add": _a_epic_add,
    "epic_list": _a_epic_list,
    "epic_del": _a_epic_del,
    "story_add": _a_story_add,
    "story_list": _a_story_list,
    "story_show": _a_story_show,
    "story_update": _a_story_update,
    "story_del": _a_story_del,
    "task_add": _a_task_add,
    "task_update": _a_task_update,
    "task_list": _a_task_list,
    "task_del": _a_task_del,
    "ticket_add": _a_ticket_add,
    "assign": _a_assign,
    "comment": _a_comment,
    "find": _a_find,
    "ticket_show": _a_ticket_show,
    "history": _a_history,
    "sprint_start": _a_sprint_start,
    "sprint_add": _a_sprint_add,
    "sprint_remove": _a_sprint_remove,
    "sprint_status": _a_sprint_status,
    "sprint_end": _a_sprint_end,
}


def scrum(args: dict[str, Any]) -> str:
    """Entry point for the ``scrum`` tool. Never raises; errors are returned
    as strings so the model can correct and retry."""
    action = str(args.get("action") or "").strip()
    if not action:
        return f"Error: action is required. One of: {', '.join(ACTIONS)}."
    if action not in _ACTION_FUNCS:
        return f"Error: unknown action {action!r}. One of: {', '.join(ACTIONS)}."

    paused = _read_board(_BOARD) or _read_board(_LEGACY_BOARD)
    if paused is not None and not paused["meta"].get("enabled", True):
        return ("Scrum tracking is switched off for this project (the user's "
                "choice). Do not re-enable it yourself — the user can with "
                "/scrum on. Continue without the board.")

    board = load_board()
    if board is None:
        if action != "init":
            return (f"No scrum board yet at {_BOARD}. "
                    "Create one with scrum(action='init', project='<name>').")
        board = new_board(str(args.get("project") or "").strip())

    board, message, changed = _ACTION_FUNCS[action](board, args)
    if changed and not message.startswith(("Error:", "Refused:")):
        _log(board, action, message)
        try:
            save_board(board)
        except OSError as e:
            return f"Error saving board: {e}"
    return message
