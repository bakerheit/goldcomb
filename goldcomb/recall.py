"""Recall: an agent's awareness of its own (and its team's) past threads.

Reads the vendor-neutral interchange files in ``<project>/.ai/threads/`` —
the only store that records *which agent* held each conversation — so recall
sees every tool's history, not just goldcomb's. Three operations, exposed as
the ``recall`` tool: ``list`` recent threads, ``search`` full text, ``read``
one thread. All default to the acting agent's own history; ``all=true``
widens to every agent (handovers, "what did the planner decide?").

``digest()`` renders the short own-history block for the system prompt, so an
agent starts each session knowing what it worked on before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import identity, scrum
from .threads import ai_threads_dir


@dataclass
class _Head:
    path: Path
    id: str
    title: str
    updated: str
    agent: str
    count: int


def _headers(cwd: Path | None = None) -> list[_Head]:
    d = ai_threads_dir(cwd)
    if not d.is_dir():
        return []
    out: list[_Head] = []
    for f in d.glob("*.jsonl"):
        try:
            lines = f.read_text().splitlines()
            header = json.loads(lines[0]) if lines else None
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(header, dict) or header.get("type") != "thread":
            continue
        out.append(_Head(
            path=f,
            id=str(header.get("id") or f.stem),
            title=str(header.get("title") or "(untitled)"),
            updated=str(header.get("updated") or ""),
            agent=str(header.get("agent") or "?"),
            count=max(0, len(lines) - 1),
        ))
    out.sort(key=lambda h: h.updated, reverse=True)
    return out


def _mine(heads: list[_Head], agent: str, all_agents: bool) -> list[_Head]:
    """The acting agent's own threads, or everyone's when ``all_agents``.

    "Own" is identity-matched, not string-equal: a bare ``==`` was the NEXA-26
    root cause (history invisible whenever the header carried a legacy or
    sub-agent name). The rule mirrors the app's ChatView filter — the header
    names this agent (with pre-rename aliases) and is not a sub-agent thread.
    A lead's workers surface only under ``all=true``, which returns everything.
    """
    if all_agents:
        return heads
    return [h for h in heads
            if identity.matches(agent, h.agent) and not identity.is_subagent(h.agent)]


def _stamp(updated: str) -> str:
    return updated[:16].replace("T", " ")


def _messages(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return []
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(m, dict) and m.get("content"):
            out.append(m)
    return out


def list_recent(agent: str | None = None, all_agents: bool = False,
                limit: int = 10) -> str:
    who = agent or scrum.CURRENT_AGENT
    heads = _mine(_headers(), who, all_agents)[:max(1, limit)]
    if not heads:
        scope = "any agent" if all_agents else f"@{who}"
        return f"(no saved conversations for {scope} in this project)"
    return "\n".join(
        f"{h.id}  [{_stamp(h.updated)}] @{h.agent}  {h.title}  ({h.count} msgs)"
        for h in heads
    )


def read_thread(id_prefix: str, max_chars: int = 6000) -> str:
    id_prefix = (id_prefix or "").strip()
    if not id_prefix:
        return "Error: read requires id=<thread id or prefix> (see action='list')."
    matches = [h for h in _headers() if h.id.startswith(id_prefix)]
    if not matches:
        return f"Error: no thread matches {id_prefix!r}."
    if len(matches) > 1:
        opts = ", ".join(h.id for h in matches[:6])
        return f"Error: {id_prefix!r} is ambiguous ({opts})."
    head = matches[0]
    lines = [f"Thread {head.id}  [{_stamp(head.updated)}] @{head.agent}: {head.title}"]
    for m in _messages(head.path):
        text = " ".join(str(m.get("content", "")).split())
        lines.append(f"{m.get('role', '?')}: {text}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n… [truncated; thread has {head.count} messages]"
    return out


def search(query: str, agent: str | None = None, all_agents: bool = False,
           limit: int = 12) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "Error: search requires query=<text>."
    who = agent or scrum.CURRENT_AGENT
    hits: list[str] = []
    for head in _mine(_headers(), who, all_agents):
        if q in head.title.lower():
            hits.append(f"{head.id}  [title] {head.title}")
        for m in _messages(head.path):
            text = " ".join(str(m.get("content", "")).split())
            low = text.lower()
            pos = low.find(q)
            if pos < 0:
                continue
            lo = max(0, pos - 60)
            snippet = ("…" if lo else "") + text[lo:pos + len(q) + 60] + \
                      ("…" if pos + len(q) + 60 < len(text) else "")
            hits.append(f"{head.id}  [{m.get('role', '?')}] {snippet}")
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if not hits:
        scope = "any agent's" if all_agents else f"@{who}'s"
        return f"(nothing matches {query!r} in {scope} conversations)"
    return "\n".join(hits[:limit])


def digest(agent: str | None = None, limit: int = 5,
           exclude_id: str | None = None) -> str | None:
    """The system-prompt block: this agent's most recent threads, or None."""
    who = agent or scrum.CURRENT_AGENT
    heads = [h for h in _mine(_headers(), who, False) if h.id != exclude_id]
    if not heads:
        return None
    return "\n".join(
        f"- [{_stamp(h.updated)}] {h.title} ({h.count} msgs, id {h.id})"
        for h in heads[:max(1, limit)]
    )


def recall_tool(args: dict) -> str:
    """Entry point for the ``recall`` tool. Never raises."""
    action = str(args.get("action") or "").strip().lower()
    all_agents = bool(args.get("all"))
    try:
        limit = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    if action == "list":
        return list_recent(all_agents=all_agents, limit=limit)
    if action == "read":
        return read_thread(str(args.get("id") or ""))
    if action == "search":
        return search(str(args.get("query") or ""), all_agents=all_agents,
                      limit=limit)
    return "Error: action must be one of list, read, search."
