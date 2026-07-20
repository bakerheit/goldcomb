"""Agent identity matching (NEXA-30 model, NEXA-31 implementation).

Recall's "mine" filter used a bare ``header.agent == name``, which was the
NEXA-26 root cause: an agent's own history went invisible the moment a header
carried a legacy (pre-rename) or sub-agent name. These tests pin the shared
matching rule and its integration into recall — and must stay in lockstep with
the Swift ``AgentIdentity`` enum, since one side writes what the other reads.
"""

import json
import os

from goldcomb import identity, recall
from goldcomb.threads import ai_threads_dir


# -- the matching rule -------------------------------------------------------

def test_exact_name_matches():
    assert identity.matches("Quill", "Quill")


def test_unrelated_names_do_not_match():
    assert not identity.matches("Quill", "Ada")


def test_legacy_tool_name_alias_is_bidirectional():
    """Pre-rename threads stamped 'nexais' belong to today's 'goldcomb'."""
    assert identity.matches("goldcomb", "nexais")
    assert identity.matches("nexais", "goldcomb")


def test_tool_name_alias_does_not_leak_to_real_agents():
    """The alias is only among the tool names — a named agent stays distinct."""
    assert not identity.matches("Quill", "nexais")
    assert not identity.matches("goldcomb", "Quill")


def test_subagent_label_alias_across_eras():
    assert identity.matches("goldcomb-subagent:packer",
                            "nexais-subagent:packer")
    # Different labels are different people, same era or not.
    assert not identity.matches("goldcomb-subagent:packer",
                                "goldcomb-subagent:linter")


def test_is_subagent_recognizes_both_eras():
    assert identity.is_subagent("goldcomb-subagent:x")
    assert identity.is_subagent("nexais-subagent:x")
    assert not identity.is_subagent("goldcomb")
    assert not identity.is_subagent("Quill")


def test_equivalents_of_plain_name_is_just_itself():
    assert identity.equivalents("Ada") == {"Ada"}


# -- recall integration ------------------------------------------------------

def _write_thread(cwd, tid: str, agent: str) -> None:
    d = ai_threads_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    header = {"type": "thread", "format": "goldcomb.ai-thread",
              "id": tid, "title": f"thread {tid}", "updated": f"2026-07-19T10:{tid[-2:]}",
              "agent": agent}
    body = {"role": "user", "content": f"hello from {agent}"}
    (d / f"{tid}.jsonl").write_text(
        json.dumps(header) + "\n" + json.dumps(body) + "\n")


def test_recall_own_history_includes_legacy_named_threads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_thread(tmp_path, "t01", agent="goldcomb")
    _write_thread(tmp_path, "t02", agent="nexais")       # pre-rename, same agent
    _write_thread(tmp_path, "t03", agent="Quill")        # someone else

    out = recall.list_recent(agent="goldcomb")
    assert "t01" in out and "t02" in out       # both eras of this agent
    assert "t03" not in out                     # not another agent's


def test_recall_own_history_excludes_subagent_threads(tmp_path, monkeypatch):
    """A lead's workers' threads are provenance-stamped and belong to the
    worker, not the lead's personal history — they need all=true."""
    monkeypatch.chdir(tmp_path)
    _write_thread(tmp_path, "t01", agent="goldcomb")
    _write_thread(tmp_path, "t02", agent="goldcomb-subagent:packer")

    own = recall.list_recent(agent="goldcomb")
    assert "t01" in own and "t02" not in own

    everyone = recall.list_recent(agent="goldcomb", all_agents=True)
    assert "t01" in everyone and "t02" in everyone


def test_digest_excludes_other_agents_and_subagents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_thread(tmp_path, "t01", agent="Ada")
    _write_thread(tmp_path, "t02", agent="nexais-subagent:w")
    _write_thread(tmp_path, "t03", agent="Quill")

    digest = recall.digest(agent="Ada")
    assert digest is not None and "t01" in digest
    assert "t02" not in digest and "t03" not in digest
