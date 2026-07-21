"""Read-only git introspection, provider-agnostic and structured.

Pure Python functions (mirroring the shape of goldcomb.tools) that shell out to
``git`` with an argv-only, timeout-bounded, cwd-scoped subprocess — never
``shell=True``, never ``cwd=None``. Each returns STRUCTURED data (dicts/lists),
and every failure mode (git missing, not-a-repo, empty/unborn repo) comes back
as a clean ``{"error": ...}`` result rather than a traceback.

The agent-facing string wrappers live in goldcomb.tools; the macOS ``--serve``
protocol calls :func:`git_status` directly (see goldcomb.server).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

# Keep large diffs from blowing the model's context — same ceiling tools.py
# uses for every other tool result.
MAX_OUTPUT = 30_000
_TIMEOUT = 15  # seconds; git introspection is fast, a hang is a bug


def _git_path() -> str | None:
    """Locate the git executable once. Returns None if git is not installed."""
    return shutil.which("git")


def _run(args: list[str], project_dir: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``project_dir``. ARGV-only, always a timeout."""
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def _is_repo(project_dir: str) -> bool:
    try:
        proc = _run(["rev-parse", "--is-inside-work-tree"], project_dir)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _guard(project_dir: str) -> dict[str, str] | None:
    """Common precondition check. Returns an error dict, or None if all good."""
    if _git_path() is None:
        return {"error": "git is not installed or not on PATH"}
    if not _is_repo(project_dir):
        return {"error": f"not a git repository: {project_dir}"}
    return None


def _truncate(text: str) -> tuple[str, bool]:
    """Cap text at MAX_OUTPUT. Returns (text, truncated) so callers can
    surface the cap explicitly (the --serve protocol emits it as a flag)."""
    if len(text) > MAX_OUTPUT:
        return (
            text[:MAX_OUTPUT] + f"\n… [truncated, {len(text)} chars total]",
            True,
        )
    return text, False


# ---- status -----------------------------------------------------------------

def _parse_porcelain(lines: list[str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Parse ``git status --porcelain=v1 --branch`` output."""
    branch: dict[str, Any] = {"branch": None, "ahead": 0, "behind": 0}
    files: list[dict[str, str]] = []
    for line in lines:
        if line.startswith("## "):
            info = line[3:]
            # e.g. "main...origin/main [ahead 1, behind 2]" or
            # "No commits yet on main" or "HEAD (no branch)"
            name = info.split("...", 1)[0].strip()
            if name.startswith("No commits yet on "):
                name = name[len("No commits yet on "):].strip()
            branch["branch"] = name
            if "[" in info:
                bracket = info[info.index("[") + 1: info.rindex("]")]
                for part in bracket.split(","):
                    part = part.strip()
                    if part.startswith("ahead "):
                        branch["ahead"] = int(part[len("ahead "):])
                    elif part.startswith("behind "):
                        branch["behind"] = int(part[len("behind "):])
            continue
        if len(line) < 3:
            continue
        xy = line[:2]
        path = line[3:]
        # Handle rename "orig -> new": report the new path.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if xy == "??":
            status = "untracked"
        elif xy[0] not in (" ", "?"):
            # Something staged in the index. If the worktree also differs
            # (xy[1] != ' '), it's both — but staged is the salient state.
            status = "staged"
        else:
            status = "unstaged"
        files.append({"path": path, "status": status})
    return branch, files


def git_status(project_dir: str) -> dict[str, Any]:
    """{branch, ahead, behind, files:[{path, status}]} or {error}."""
    err = _guard(project_dir)
    if err:
        return err
    try:
        proc = _run(["status", "--porcelain=v1", "--branch"], project_dir)
    except subprocess.TimeoutExpired:
        return {"error": "git status timed out"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"git status failed: {e}"}
    if proc.returncode != 0:
        return {"error": f"git status failed: {proc.stderr.strip()}"}
    branch, files = _parse_porcelain(proc.stdout.splitlines())
    return {
        "branch": branch["branch"],
        "ahead": branch["ahead"],
        "behind": branch["behind"],
        "files": files,
    }


# ---- diff -------------------------------------------------------------------

def git_diff(
    project_dir: str, path: str | None = None, staged: bool = False
) -> dict[str, Any]:
    """Unified-diff text for the working tree (or the index if staged=True).

    Returns {diff: str, truncated: bool} (diff possibly capped), or {error}.
    An untracked new file yields no diff from ``git diff``; we detect that and
    report a clear "new file, no diff" note instead of a blank string.
    """
    err = _guard(project_dir)
    if err:
        return err

    args = ["diff"]
    if staged:
        args.append("--cached")
    if path is not None:
        args += ["--", path]

    try:
        proc = _run(args, project_dir)
    except subprocess.TimeoutExpired:
        return {"error": "git diff timed out"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"git diff failed: {e}"}
    if proc.returncode != 0:
        return {"error": f"git diff failed: {proc.stderr.strip()}"}

    diff = proc.stdout

    # Untracked file: git diff shows nothing. Detect and report it clearly
    # (a blank diff would look like "no changes" to the model).
    if not diff.strip() and path is not None and not staged:
        if _is_untracked(project_dir, path):
            return {
                "diff": f"{path}: new file, no diff (untracked — use git add to stage)",
                "truncated": False,
            }

    if not diff.strip():
        return {"diff": "(no changes)", "truncated": False}
    text, truncated = _truncate(diff)
    return {"diff": text, "truncated": truncated}


def _is_untracked(project_dir: str, path: str) -> bool:
    try:
        proc = _run(["status", "--porcelain=v1", "--", path], project_dir)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return any(line.startswith("??") for line in proc.stdout.splitlines())


# ---- log --------------------------------------------------------------------

# Field separator unlikely to appear in commit metadata.
_LOG_SEP = "\x1f"
_LOG_FMT = _LOG_SEP.join(["%H", "%an", "%aI", "%s"])


def git_log(project_dir: str, n: int = 20) -> Any:
    """List of {hash, author, date, subject}, newest first, or {error}.

    An empty repo (unborn HEAD) has no commits: ``git log`` exits non-zero
    there — we return an empty list, not an error, since "no commits" is a
    valid state, not a failure.
    """
    err = _guard(project_dir)
    if err:
        return err
    try:
        proc = _run(
            ["log", f"-n{int(n)}", f"--pretty=format:{_LOG_FMT}"], project_dir
        )
    except subprocess.TimeoutExpired:
        return {"error": "git log timed out"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"git log failed: {e}"}
    if proc.returncode != 0:
        # Unborn HEAD / empty repo → no commits yet. Not an error.
        if _is_unborn(project_dir):
            return []
        return {"error": f"git log failed: {proc.stderr.strip()}"}
    commits: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        parts = line.split(_LOG_SEP)
        if len(parts) != 4:
            continue
        h, author, iso_date, subject = parts
        commits.append(
            {"hash": h, "author": author, "date": iso_date, "subject": subject}
        )
    return commits


def _is_unborn(project_dir: str) -> bool:
    """True when HEAD points at a branch with no commits (empty repo)."""
    try:
        proc = _run(["rev-parse", "--verify", "HEAD"], project_dir)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode != 0


# ---- branch -----------------------------------------------------------------

def git_branch(project_dir: str) -> dict[str, Any]:
    """{current, branches:[...]} or {error}. current is None on an unborn HEAD
    or a detached HEAD."""
    err = _guard(project_dir)
    if err:
        return err
    try:
        proc = _run(
            ["branch", "--format=%(refname:short)"], project_dir
        )
    except subprocess.TimeoutExpired:
        return {"error": "git branch timed out"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"git branch failed: {e}"}
    if proc.returncode != 0:
        return {"error": f"git branch failed: {proc.stderr.strip()}"}
    branches = [b.strip() for b in proc.stdout.splitlines() if b.strip()]

    current: str | None = None
    try:
        cur = _run(["symbolic-ref", "--quiet", "--short", "HEAD"], project_dir)
        if cur.returncode == 0:
            current = cur.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        current = None
    return {"current": current, "branches": branches}
