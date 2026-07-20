"""Human names for agents — every agent gets a First Last, however created.

A named colleague ("Maya Trellis is on GOLD-7") reads better than a slug on
the board, in the team tree, and in thread history. The generator is a small
curated pool: distinct, pronounceable, no real-person collisions intended.

``humanize(label)`` is the policy gate: given whatever a creation path has
(a deploy label, a blank field), it returns something human — keeping names
that already look like a person, and otherwise minting one, with the original
label folded in parenthetically so functional intent survives ("Ines Vale
(retry-worker)" claims tickets legibly *and* memorably).
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

#: Per-project roster: functional label -> the human identity minted for it.
#: Same label = same person on every deploy, so a redeployed worker keeps its
#: name, its ticket history, and its memory file.
_ROSTER = Path(".ai") / "agents" / "roster.json"

FIRST = [
    "Ada", "Amos", "Anouk", "Aria", "Basil", "Beatrix", "Callum", "Cleo",
    "Dara", "Della", "Edwin", "Effie", "Felix", "Freya", "Gideon", "Greta",
    "Hollis", "Ines", "Ivo", "Juniper", "Kai", "Lena", "Linus", "Maeve",
    "Marlowe", "Maya", "Nadia", "Nico", "Opal", "Otis", "Petra", "Quill",
    "Rafael", "Romy", "Silas", "Sonia", "Tamsin", "Theo", "Vera", "Wren",
]

LAST = [
    "Ambrose", "Ashwood", "Beckett", "Birch", "Calloway", "Cardew", "Danforth",
    "Eastley", "Fenn", "Foxglove", "Gable", "Greenlaw", "Hale", "Harlow",
    "Ibsen", "Juno", "Kestrel", "Larkspur", "Mercer", "Moss", "Northgate",
    "Oakes", "Pemberly", "Quimby", "Rook", "Sable", "Sorrel", "Thistle",
    "Trellis", "Umber", "Vale", "Wilder", "Winslow", "Yarrow", "Zephyr",
]


def random_name(rng: random.Random | None = None) -> str:
    r = rng or random
    return f"{r.choice(FIRST)} {r.choice(LAST)}"


def looks_human(name: str) -> bool:
    """Two-plus capitalized words, letters only — 'Maya Trellis' yes,
    'retry-worker' or 'agent 3' no."""
    words = (name or "").strip().split()
    return (len(words) >= 2
            and all(re.fullmatch(r"[A-Z][a-zA-Z'’-]+", w) for w in words))


def _roster_load() -> dict[str, str]:
    try:
        data = json.loads(_ROSTER.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _roster_save(roster: dict[str, str]) -> None:
    try:
        _ROSTER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ROSTER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(roster, indent=2) + "\n")
        tmp.replace(_ROSTER)
    except OSError:
        pass  # identity persistence is best-effort; the deploy still runs


def humanize(label: str | None = None, rng: random.Random | None = None) -> str:
    """A human identity for the agent deployed as `label` (may be empty).

    Human-looking labels pass through untouched. Functional labels resolve
    through the project roster: the FIRST deploy of a label mints a person
    ("Ada Gable (retry-worker)") and remembers it; every later deploy with
    the same label is the same person — name, tickets, and memory carry.
    """
    label = " ".join((label or "").split())
    if looks_human(label):
        return label
    generic = label.lower() in ("", "agent", "worker", "subagent", "sub-agent")
    key = label.lower() or "worker"
    roster = _roster_load()
    if key in roster:
        return roster[key]
    taken = set(roster.values())
    for _ in range(24):
        name = random_name(rng)
        full = name if generic else f"{name} ({label})"
        if full not in taken:
            break
    roster[key] = full
    _roster_save(roster)
    return full
