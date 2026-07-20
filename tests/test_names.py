"""Human agent names (goldcomb/names.py)."""

import random

from goldcomb.names import FIRST, LAST, humanize, looks_human, random_name


def test_random_name_shape():
    rng = random.Random(7)
    name = random_name(rng)
    first, last = name.split()
    assert first in FIRST and last in LAST


def test_looks_human():
    assert looks_human("Maya Trellis")
    assert looks_human("Anne-Marie O'Neill")
    assert not looks_human("retry-worker")
    assert not looks_human("agent 3")
    assert not looks_human("planner")
    assert not looks_human("")


def test_humanize_passthrough_and_minting():
    rng = random.Random(7)
    assert humanize("Maya Trellis", rng) == "Maya Trellis"
    minted = humanize("retry-worker", rng)
    assert "(retry-worker)" in minted and looks_human(minted.split(" (")[0])
    plain = humanize("", rng)
    assert looks_human(plain) and "(" not in plain
    assert "(" not in humanize("agent", rng)


def test_roster_reuses_identity_per_label(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = humanize("retry-worker")
    again = humanize("retry-worker")
    assert first == again                       # same label = same person
    other = humanize("ui-worker")
    assert other != first                       # new specialty = new person
    assert humanize("Maya Trellis") == "Maya Trellis"   # humans bypass roster
    # the roster is plain JSON on disk, editable like everything in .ai
    import json
    roster = json.loads((tmp_path / ".ai/agents/roster.json").read_text())
    assert roster["retry-worker"] == first
