"""Built-in agentic tools: filesystem + shell, exposed to any provider.

Each tool has a JSON-Schema spec (used to advertise it to the model) and a
Python implementation. Tools that mutate state (write/edit/bash) are flagged
``dangerous`` so the CLI can ask for confirmation before running them.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .providers import ToolSpec
from .scrum import is_enabled as _scrum_enabled
from .scrum import scrum as _scrum_run


def _memory_run(args):
    from . import memory as _memory_mod
    return _memory_mod.memory_tool(args)


def _recall_run(args):
    from . import recall as _recall_mod
    return _recall_mod.recall_tool(args)


def _chat_run(args):
    from . import chats as _chats_mod
    return _chats_mod.chat_tool(args)


MAX_OUTPUT = 30_000  # cap tool output returned to the model
# Refuse / abort a shell command if free disk falls below this floor. A runaway
# copy that would fill the disk is killed here instead of taking the machine down.
MIN_FREE_MB = int(os.environ.get("GOLDCOMB_MIN_FREE_MB", "500"))

# Unambiguously catastrophic commands we refuse to run at all.
_CATASTROPHIC = [
    (re.compile(r"\brm\b[^|;&\n]*\s-[a-zA-Z]*[rf][a-zA-Z]*[^|;&\n]*"
                r"\s(/|/\*|~|~/|\$HOME|/Users/?|/home/?|\.\.)(\s|$)"),
     "rm -rf targeting a root/home/parent path"),
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*\|\s*:[^}]*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bdd\b[^|;&\n]*\bof=/dev/"), "dd writing to a device"),
    (re.compile(r"\bmkfs\b"), "mkfs (formats a filesystem)"),
    (re.compile(r">\s*/dev/(sd|disk|nvme|hd)"), "writing to a raw disk device"),
]


def catastrophic_reason(command: str) -> str | None:
    for pat, why in _CATASTROPHIC:
        if pat.search(command):
            return why
    return None


def _free_mb(path: str) -> int:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass


@dataclass
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any]], str]
    dangerous: bool = False

    @property
    def name(self) -> str:
        return self.spec.name


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n… [truncated, {len(text)} chars total]"
    return text


def _read_file(args: dict[str, Any]) -> str:
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return f"Error: file not found: {path}"
    if path.is_dir():
        return f"Error: {path} is a directory (use list_dir)"
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return f"Error reading {path}: {e}"
    start = int(args.get("offset", 0) or 0)
    limit = args.get("limit")
    lines = text.splitlines()
    if start or limit:
        end = start + int(limit) if limit else len(lines)
        lines = lines[start:end]
    numbered = "\n".join(f"{i + start + 1}\t{ln}" for i, ln in enumerate(lines))
    return _truncate(numbered) if numbered else "(empty file)"


def _write_file(args: dict[str, Any]) -> str:
    path = Path(args["path"]).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"])
    except OSError as e:
        return f"Error writing {path}: {e}"
    n = len(args["content"].splitlines())
    return f"Wrote {n} lines to {path}"


def _edit_file(args: dict[str, Any]) -> str:
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return f"Error: file not found: {path}"
    try:
        text = path.read_text()
    except OSError as e:
        return f"Error reading {path}: {e}"
    old, new = args["old_string"], args["new_string"]
    count = text.count(old)
    if count == 0:
        return "Error: old_string not found in file. Read the file first to copy exact text."
    if count > 1 and not args.get("replace_all"):
        return f"Error: old_string appears {count} times. Make it unique or set replace_all=true."
    new_text = text.replace(old, new)
    try:
        path.write_text(new_text)
    except OSError as e:
        return f"Error writing {path}: {e}"
    n = count if args.get("replace_all") else 1
    return f"Replaced {n} occurrence(s) in {path}.\n{_changed_region(new_text, new)}"


def _changed_region(new_text: str, inserted: str) -> str:
    """Show the edited region (line-numbered, ±3 lines) so the model can see
    the actual result — indentation included — instead of editing blind."""
    idx = new_text.find(inserted)
    lines = new_text.splitlines()
    if idx == -1:
        return "Updated file."
    start_line = new_text.count("\n", 0, idx)
    end_line = start_line + inserted.count("\n")
    lo = max(0, start_line - 3)
    hi = min(len(lines), end_line + 4)
    body = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(lo, hi))
    return f"Updated region:\n{body}"


def _list_dir(args: dict[str, Any]) -> str:
    path = Path(args.get("path", ".")).expanduser()
    if not path.exists():
        return f"Error: path not found: {path}"
    if path.is_file():
        return str(path)
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"Error listing {path}: {e}"
    lines = [f"{'d' if p.is_dir() else 'f'}  {p.name}{'/' if p.is_dir() else ''}" for p in entries]
    return _truncate("\n".join(lines)) if lines else "(empty directory)"


def _run_bash(args: dict[str, Any]) -> str:
    cmd = args["command"]
    timeout = int(args.get("timeout", 120) or 120)

    reason = catastrophic_reason(cmd)
    if reason:
        return f"Refused: this command looks destructive ({reason}). Not running it."
    try:
        cwd = os.getcwd()
    except OSError:
        return "Error: the current working directory is no longer accessible."
    free = _free_mb(cwd)
    if 0 <= free < MIN_FREE_MB:
        return (
            f"Refused: only {free} MB free on disk (floor {MIN_FREE_MB} MB). "
            "Free up space before running commands that could write more."
        )

    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, start_new_session=True,  # own process group → killable as a tree
        )
    except Exception as e:  # pragma: no cover
        return f"Error running command: {e}"

    # Disk sentinel: if free space crosses the floor mid-command (e.g. a runaway
    # copy), kill the whole process tree before it fills the disk.
    killed: dict[str, str | None] = {"reason": None}

    def _watchdog() -> None:
        while proc.poll() is None:
            f = _free_mb(cwd)
            if 0 <= f < MIN_FREE_MB:
                killed["reason"] = f"free disk fell below {MIN_FREE_MB} MB"
                _kill_group(proc)
                return
            time.sleep(0.5)

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        return _truncate(_bash_output(out, err, None) + f"\n[killed: timed out after {timeout}s]")
    if killed["reason"]:
        return _truncate(
            _bash_output(out, err, None)
            + f"\n[KILLED: {killed['reason']} — aborted to protect the disk]"
        )
    return _truncate(_bash_output(out, err, proc.returncode)) or "(no output)"


# Injected by the CLI at startup (it owns config, providers, and rendering).
# Kept as hooks so this module stays free of cli/provider imports.
_agent_runner: Callable[[dict[str, Any]], str] | None = None
_ask_runner: Callable[[dict[str, Any]], str] | None = None


def set_agent_runner(runner: Callable[[dict[str, Any]], str] | None) -> None:
    global _agent_runner
    _agent_runner = runner


def set_ask_runner(runner: Callable[[dict[str, Any]], str] | None) -> None:
    global _ask_runner
    _ask_runner = runner


def _deploy_agent(args: dict[str, Any]) -> str:
    if _agent_runner is None:
        return "Error: sub-agents are not available in this context."
    return _agent_runner(args)


def _ask_user(args: dict[str, Any]) -> str:
    if _ask_runner is None:
        return (
            "Error: no interactive user is available in this context. "
            "Proceed with your best judgment and state the assumption you made."
        )
    return _ask_runner(args)


def _bash_output(out: str, err: str, code: int | None) -> str:
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if code not in (0, None):
        parts.append(f"[exit code {code}]")
    return "\n".join(parts)


BUILTIN_TOOLS: list[Tool] = [
    Tool(
        ToolSpec(
            name="read_file",
            description="Read a text file from the local filesystem. "
                        "Returns line-numbered content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "integer",
                               "description": "Line number to start from (0-based)"},
                    "limit": {"type": "integer", "description": "Max number of lines to read"},
                },
                "required": ["path"],
            },
        ),
        _read_file,
    ),
    Tool(
        ToolSpec(
            name="write_file",
            description="Write (create or overwrite) a text file with the given content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        _write_file,
        dangerous=True,
    ),
    Tool(
        ToolSpec(
            name="edit_file",
            description="Replace an exact string in a file. old_string must match "
                        "uniquely unless replace_all is true.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
        _edit_file,
        dangerous=True,
    ),
    Tool(
        ToolSpec(
            name="list_dir",
            description="List the contents of a directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string",
                                        "description": "Directory path (default '.')"}},
            },
        ),
        _list_dir,
    ),
    Tool(
        ToolSpec(
            name="run_bash",
            description="Run a shell command and return its combined stdout/stderr. "
                        "Use for git, tests, builds, grep, etc.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer",
                                "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        ),
        _run_bash,
        dangerous=True,
    ),
    Tool(
        ToolSpec(
            name="deploy_agent",
            description=(
                "Deploy an autonomous sub-agent to complete one self-contained task. "
                "It gets a fresh context and the same file/shell tools (but cannot "
                "deploy further agents), works without supervision, and returns a "
                "final report. Use it for parallelizable or context-heavy subtasks: "
                "broad codebase searches, bulk mechanical edits, long test runs, or "
                "noisy exploration you want kept out of your own context. The brief "
                "must be complete and standalone — the sub-agent cannot see this "
                "conversation or ask questions. Optionally pick a different "
                "configured provider and/or model for it; by default it runs on the "
                "session's current ones."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Complete standalone brief: the goal, all "
                        "needed context (paths, constraints, commands), and what "
                        "the final report should contain.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Short display name, e.g. 'test-runner'",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Configured provider name (default: current)",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model id for the sub-agent (default: the "
                        "session's current model)",
                    },
                },
                "required": ["task"],
            },
        ),
        _deploy_agent,
        dangerous=True,
    ),
    Tool(
        ToolSpec(
            name="ask_user",
            description=(
                "Ask the user up to 4 clarifying questions and wait for their "
                "answers. Use ONLY when blocked on a decision that is genuinely "
                "the user's to make — preferences, scope, or hard-to-reverse "
                "choices you cannot resolve from the request, the code, or "
                "sensible defaults. Never ask about things you can discover with "
                "your other tools. Offer 2-4 distinct options per question when "
                "natural; the user can always answer in free text instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "1-4 questions to ask.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "The complete question, ending "
                                    "with a question mark.",
                                },
                                "header": {
                                    "type": "string",
                                    "description": "Very short topic label, e.g. "
                                    "'Database'",
                                },
                                "options": {
                                    "type": "array",
                                    "description": "2-4 suggested answers.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string"},
                                            "description": {
                                                "type": "string",
                                                "description": "What choosing this "
                                                "implies (trade-offs).",
                                            },
                                        },
                                        "required": ["label"],
                                    },
                                },
                                "multi_select": {
                                    "type": "boolean",
                                    "description": "Allow choosing several options.",
                                },
                            },
                            "required": ["question"],
                        },
                    },
                },
                "required": ["questions"],
            },
        ),
        _ask_user,
    ),
    Tool(
        ToolSpec(
            name="scrum",
            description=(
                "A JIRA-style scrum board for planning and tracking work in this "
                "project (epics -> stories -> tasks, plus sprints), persisted at "
                ".ai/scrum/board.json. Every item is a ticket with an id like "
                "DEMO-7 under the board's project key. Tickets have assignees: "
                "moving a task to in_progress auto-assigns it to you, and 'assign' "
                "hands a ticket to a named agent, so the board shows who works on "
                "what. Start with action='show', or action='init' if none exists. "
                "Typical flow: ticket_add (one call: story + first task, epic "
                "auto-created if needed, sprint=true joins the active sprint) "
                "or epic_add -> story_add -> task_add to break work down, then "
                "task_update to move tickets todo -> in_progress -> done."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Which operation to perform",
                        "enum": [
                            "init", "show", "burndown",
                            "epic_add", "epic_list", "epic_del",
                            "story_add", "story_list", "story_show", "story_update",
                            "story_del",
                            "task_add", "task_update", "task_list", "task_del",
                            "ticket_add",
                            "assign", "comment", "find", "ticket_show", "history",
                            "sprint_start", "sprint_add", "sprint_remove",
                            "sprint_status", "sprint_end",
                        ],
                    },
                    "project": {"type": "string", "description": "Project name (init)"},
                    "key": {"type": "string", "description": "Ticket prefix like DEMO "
                            "(init; derived from the project name if omitted)"},
                    "ticket": {"type": "string", "description": "Any ticket id (assign)"},
                    "assignee": {"type": "string", "description": "Agent/person to assign "
                                 "(assign/task_add/task_update/story_add; assign defaults "
                                 "to yourself)"},
                    "title": {"type": "string",
                              "description": "Title for epic/story/task add or rename"},
                    "epic": {"type": "string",
                             "description": "Epic id, e.g. E1-x "
                                            "(story_add/story_update/story_list)"},
                    "story": {"type": "string",
                              "description": "Story id, e.g. S1-x "
                                             "(story_show/story_update/task_add/sprint_add)"},
                    "task": {"type": "string", "description": "Task id, e.g. T1-x (task_update)"},
                    "status": {
                        "type": "string",
                        "description": "New status (task_update) or filter (task_list)",
                        "enum": ["todo", "in_progress", "blocked", "done"],
                    },
                    "priority": {
                        "type": "string",
                        "description": "Story priority",
                        "enum": ["low", "medium", "high"],
                    },
                    "points": {"type": "integer", "description": "Story points for a task"},
                    "goal": {"type": "string", "description": "Sprint goal (sprint_start)"},
                    "notes": {"type": "string", "description": "Free-form notes (story/task)"},
                    "text": {"type": "string", "description": "Comment body (comment)"},
                    "query": {"type": "string", "description": "Search text (find)"},
                    "label": {"type": "string", "description": "Label filter (task_list)"},
                    "labels": {"type": "string", "description": "Comma-separated labels "
                               "(task_update/story_update; replaces the set)"},
                    "blocked_by": {"type": "string", "description": "Comma-separated ticket "
                                   "ids this task waits on (task_update; '' clears)"},
                    "force": {"type": "boolean", "description": "Confirm destructive "
                              "deletes (story_del/epic_del with children)"},
                    "sprint": {"type": "boolean", "description": "ticket_add: also add "
                               "the new ticket to the active sprint"},
                    "limit": {"type": "integer", "description": "Max entries (history)"},
                },
                "required": ["action"],
            },
        ),
        _scrum_run,
    ),
    Tool(
        ToolSpec(
            name="memory",
            description=(
                "Your private, persistent memory for this project "
                "(.ai/memory/<your-name>.md) — it is loaded into your system "
                "prompt every session. Use action='remember' to keep one "
                "durable fact (a decision and its reason, a user preference, "
                "a lesson learned, a project quirk); action='rewrite' to "
                "prune/reorganize the whole file; action='show' to read it. "
                "Do NOT store session scratch or anything the repo already "
                "records."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["show", "remember", "rewrite"],
                        "description": "What to do with your memory",
                    },
                    "text": {
                        "type": "string",
                        "description": "remember: the one fact to keep. "
                                       "rewrite: the full new file content "
                                       "('' clears it).",
                    },
                },
                "required": ["action"],
            },
        ),
        _memory_run,
    ),
    Tool(
        ToolSpec(
            name="recall",
            description=(
                "Search and reread past conversations in this project "
                "(.ai/threads). Defaults to YOUR own history; pass all=true "
                "to include every agent's (e.g. to pick up a teammate's "
                "handover). action='list' shows recent threads, "
                "action='search' greps them, action='read' prints one by id. "
                "Use it when the user references earlier work you don't see "
                "in the current conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "search"],
                        "description": "Which recall operation",
                    },
                    "id": {"type": "string",
                           "description": "Thread id or unique prefix (read)"},
                    "query": {"type": "string",
                              "description": "Text to find (search)"},
                    "all": {"type": "boolean",
                            "description": "Include every agent's threads, "
                                           "not just yours"},
                    "limit": {"type": "integer",
                              "description": "Max results (list/search)"},
                },
                "required": ["action"],
            },
        ),
        _recall_run,
    ),
    Tool(
        ToolSpec(
            name="chat",
            description=(
                "Message the project's other agents: group chats and direct "
                "messages (.ai/chats). Use a DM (action='dm') to ask the "
                "teammate most familiar with a module; use a group chat "
                "(action='start' with participants) for discussions like "
                "sprint planning or raising codebase concerns. The human "
                "user reads everything and is a participant in every group "
                "chat — address them as 'user'. Delivery is asynchronous: "
                "teammates are woken with your message and their replies "
                "arrive in your later turns, so post and move on — don't "
                "wait or poll. Keep messages short and substantive; don't "
                "post just to acknowledge. To share a file (a diff, log, or "
                "screenshot you just produced), pass its path in "
                "'attachments' on start/dm/post — teammates read it with "
                "read_file. Images can be attached but not yet seen by agents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "start", "dm", "post", "read", "add"],
                        "description": "list chats · start a group chat · "
                                       "dm a teammate · post to a chat · "
                                       "read a chat · add participants",
                    },
                    "id": {"type": "string",
                           "description": "Chat id or unique prefix "
                                          "(post/read/add)"},
                    "title": {"type": "string",
                              "description": "start: the chat's topic"},
                    "participants": {
                        "type": "array", "items": {"type": "string"},
                        "description": "start/add: teammate names "
                                       "(the user is included automatically)",
                    },
                    "to": {"type": "string",
                           "description": "dm: the teammate's name"},
                    "text": {"type": "string",
                             "description": "The message (start/dm/post)"},
                    "attachments": {
                        "type": "array", "items": {"type": "string"},
                        "description": "start/dm/post: workspace-relative file "
                                       "paths to attach (copied into the room; "
                                       "teammates read_file them)",
                    },
                    "last": {"type": "integer",
                             "description": "read: how many recent messages "
                                            "(default 30)"},
                },
                "required": ["action"],
            },
        ),
        _chat_run,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in BUILTIN_TOOLS}


def available_tools() -> list[Tool]:
    """The tools offered to models right now. Scrum is per-project opt-in
    (/scrum on) — projects without tracking never advertise it, so models
    don't bureaucratize repos that don't want a board."""
    return [t for t in BUILTIN_TOOLS if t.name != "scrum" or _scrum_enabled()]


def tool_specs() -> list[ToolSpec]:
    return [t.spec for t in available_tools()]


def missing_required_args(tool: Tool, args: dict[str, Any] | None) -> list[str]:
    """Required params (from the tool's schema) absent from the model's call."""
    required = tool.spec.parameters.get("required", []) or []
    have = args or {}
    return [r for r in required if r not in have]


def describe_call(name: str, args: dict[str, Any]) -> str:
    """A short one-line summary of a tool call for display."""
    if name == "run_bash":
        return f"$ {args.get('command', '')}"
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        return f"{name}({args.get('path', '')})"
    if name == "deploy_agent":
        label = args.get("label") or "agent"
        task = " ".join(str(args.get("task", "")).split())
        target = f" → {args['model']}" if args.get("model") else ""
        return f"deploy_agent[{label}{target}] {task[:70]}{'…' if len(task) > 70 else ''}"
    if name == "ask_user":
        qs = args.get("questions") or []
        first = str((qs[0] or {}).get("question", "")) if qs else ""
        extra = f" (+{len(qs) - 1} more)" if len(qs) > 1 else ""
        return f"ask_user: {first[:70]}{'…' if len(first) > 70 else ''}{extra}"
    if name == "scrum":
        action = args.get("action", "")
        target = (args.get("ticket") or args.get("task") or args.get("story")
                  or args.get("epic") or args.get("title") or "")
        return f"scrum {action} {target}".rstrip()
    inner = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return f"{name}({inner})"
