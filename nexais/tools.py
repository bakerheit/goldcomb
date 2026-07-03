"""Built-in agentic tools: filesystem + shell, exposed to any provider.

Each tool has a JSON-Schema spec (used to advertise it to the model) and a
Python implementation. Tools that mutate state (write/edit/bash) are flagged
``dangerous`` so the CLI can ask for confirmation before running them.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .providers import ToolSpec

MAX_OUTPUT = 30_000  # cap tool output returned to the model


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
    text = text.replace(old, new)
    try:
        path.write_text(text)
    except OSError as e:
        return f"Error writing {path}: {e}"
    return f"Replaced {count if args.get('replace_all') else 1} occurrence(s) in {path}"


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
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:  # pragma: no cover
        return f"Error running command: {e}"
    out = proc.stdout or ""
    err = proc.stderr or ""
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if proc.returncode != 0:
        parts.append(f"[exit code {proc.returncode}]")
    return _truncate("\n".join(parts)) if parts else "(no output)"


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


def describe_call(name: str, args: dict[str, Any]) -> str:
    """A short one-line summary of a tool call for display."""
    if name == "run_bash":
        return f"$ {args.get('command', '')}"
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        return f"{name}({args.get('path', '')})"
    inner = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return f"{name}({inner})"
