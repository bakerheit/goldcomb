"""Tests for goldcomb.git_tools — read-only, structured git introspection."""

import shutil
import subprocess

import pytest

from goldcomb import git_tools

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repo with one committed file, a.txt."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.co")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_status_clean_repo(repo):
    res = git_tools.git_status(str(repo))
    assert "error" not in res
    assert res["files"] == []
    assert res["branch"]  # a branch name is present (main/master)


def test_status_dirty_repo_distinguishes_states(repo):
    # modify a tracked file (unstaged)
    (repo / "a.txt").write_text("one\ntwo\n")
    # untracked new file
    (repo / "b.txt").write_text("untracked\n")
    # staged new file
    (repo / "c.txt").write_text("staged\n")
    _git(repo, "add", "c.txt")

    res = git_tools.git_status(str(repo))
    assert "error" not in res
    by_path = {f["path"]: f["status"] for f in res["files"]}
    assert by_path["a.txt"] == "unstaged"
    assert by_path["b.txt"] == "untracked"
    assert by_path["c.txt"] == "staged"


def test_diff_modified_file_contains_change(repo):
    (repo / "a.txt").write_text("one\nTWO_ADDED\n")
    res = git_tools.git_diff(str(repo), path="a.txt")
    assert "error" not in res
    assert "TWO_ADDED" in res["diff"]
    assert res["truncated"] is False


def test_diff_untracked_file_reports_new_file(repo):
    (repo / "new.txt").write_text("brand new\n")
    res = git_tools.git_diff(str(repo), path="new.txt")
    assert "error" not in res
    assert "new file, no diff" in res["diff"]
    assert res["truncated"] is False


def test_status_not_a_repo_returns_error_not_exception(tmp_path):
    sub = tmp_path / "not_a_repo"
    sub.mkdir()
    res = git_tools.git_status(str(sub))
    assert "error" in res
    assert "not a git repository" in res["error"]


def test_log_empty_repo_returns_empty_list(tmp_path):
    _git(tmp_path, "init", "-q")
    res = git_tools.git_log(str(tmp_path))
    assert res == []


def test_diff_truncates_huge_output(repo):
    # Commit a big file, then modify every line so the diff exceeds MAX_OUTPUT.
    original = "".join(f"line {i}\n" for i in range(5000))
    (repo / "big.txt").write_text(original)
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-q", "-m", "big")

    modified = "".join(f"changed {i}\n" for i in range(5000))
    (repo / "big.txt").write_text(modified)

    res = git_tools.git_diff(str(repo), path="big.txt")
    assert "error" not in res
    assert res["truncated"] is True
    diff = res["diff"]
    # The truncation notice is present and the body is capped near MAX_OUTPUT.
    assert "truncated" in diff
    assert len(diff) <= git_tools.MAX_OUTPUT + 100
