"""Agent identity matching for thread headers (NEXA-30 decision, NEXA-31).

The Python mirror of the macOS app's ``AgentIdentity`` enum — the two must
agree, because a thread written by one is read back by the other. The model
(documented in GOLDCOMB.md, "Agent identity model"):

- One name is one canonical identity; matching is exact **except** for the
  read-time legacy aliases from the pre-rename era: ``nexais`` ≡ ``goldcomb``
  and ``nexais-subagent:<label>`` ≡ ``goldcomb-subagent:<label>``. Aliases are
  applied only when reading — files on disk are never rewritten.
- A sub-agent's thread header carries ``<tool>-subagent:<label>`` (provenance);
  the durable person is the bare label. A lead agent does not own its workers'
  threads in its personal history — those surface only via ``recall all=true``
  or the Project tab, so ``is_subagent`` marks them for exclusion from an
  agent's own list.
"""

from __future__ import annotations

#: Tool names that are interchangeable in thread headers (pre/post rename).
_LEGACY_TOOL_NAMES = frozenset({"goldcomb", "nexais"})

#: Sub-agent id prefixes, canonical first.
_SUBAGENT_PREFIXES = ("goldcomb-subagent:", "nexais-subagent:")


def equivalents(name: str) -> set[str]:
    """Every header ``agent`` value that names the same identity as ``name``:
    itself, plus its legacy alias when it is a bare tool name or a sub-agent id
    from the other naming era."""
    out = {name}
    if name in _LEGACY_TOOL_NAMES:
        out |= set(_LEGACY_TOOL_NAMES)
    else:
        for prefix in _SUBAGENT_PREFIXES:
            if name.startswith(prefix):
                label = name[len(prefix):]
                out |= {p + label for p in _SUBAGENT_PREFIXES}
                break
    return out


def matches(name: str, header_agent: str) -> bool:
    """Does a thread whose header agent is ``header_agent`` belong to the agent
    named ``name``? Exact name match plus the legacy aliases above."""
    return header_agent in equivalents(name)


def is_subagent(header_agent: str) -> bool:
    """Does a thread header name a sub-agent (``<tool>-subagent:<label>``,
    either era)? Such threads are attributed to the worker label, not the lead,
    so they are excluded from a parent agent's own history list."""
    return any(header_agent.startswith(p) for p in _SUBAGENT_PREFIXES)
