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

MAX_OUTPUT = 30_000  # cap tool output returned to the model
# Refuse / abort a shell command if free disk falls below this floor. A runaway
# copy that would fill the disk is killed here instead of taking the machine down.
MIN_FREE_MB = int(os.environ.get("NEXAIS_MIN_FREE_MB", "500"))

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
            description="Read a text file from the local filesystem. Returns line-numbered content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start from (0-based)"},
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
            description="Replace an exact string in a file. old_string must match uniquely unless replace_all is true.",
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
                "properties": {"path": {"type": "string", "description": "Directory path (default '.')"}},
            },
        ),
        _list_dir,
    ),
    Tool(
        ToolSpec(
            name="run_bash",
            description="Run a shell command and return its combined stdout/stderr. Use for git, tests, builds, grep, etc.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        ),
        _run_bash,
        dangerous=True,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in BUILTIN_TOOLS}


def tool_specs() -> list[ToolSpec]:
    return [t.spec for t in BUILTIN_TOOLS]


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
    inner = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return f"{name}({inner})"
