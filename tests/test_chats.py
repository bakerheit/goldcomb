"""Chat rooms: storage, identity, tool actions."""

import json
from pathlib import Path

import pytest

from goldcomb import chats, scrum


@pytest.fixture(autouse=True)
def tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chats, "CHATS_DIR", Path(".ai") / "chats")
    monkeypatch.setattr(scrum, "CURRENT_AGENT", "Beatrix Winslow")
    yield tmp_path


def test_start_includes_author_and_user():
    chat_id = chats.start("Sprint planning", ["Maya Wilder", "Vera Umber"])
    header, messages = chats.load(chat_id)
    assert header["participants"] == [
        "Beatrix Winslow", "Maya Wilder", "Vera Umber", "user"]
    assert header["kind"] == "group"
    assert messages == []


def test_post_and_read_roundtrip():
    chat_id = chats.start("Standup", ["Maya Wilder"], text="Morning all")
    chats.post(chat_id, "Here — NEXA-42 shipped", author="Maya Wilder")
    header, messages = chats.load(chat_id)
    assert [m["from"] for m in messages] == ["Beatrix Winslow", "Maya Wilder"]
    rendered = chats.render(header, messages)
    assert "NEXA-42 shipped" in rendered and "Standup" in rendered


def test_labels_resolve_through_roster():
    # A functional label participant becomes the same person deploys mint.
    from goldcomb.names import humanize
    person = humanize("swift-worker-2")
    chat_id = chats.start("UI concerns", ["swift-worker-2"])
    header, _ = chats.load(chat_id)
    assert person in header["participants"]
    assert person.endswith("(swift-worker-2)")


def test_dm_is_stable_per_pair():
    out = chats.chat_tool({"action": "dm", "to": "Maya Wilder", "text": "got a sec?"})
    assert "Maya Wilder" in out
    out2 = chats.chat_tool({"action": "dm", "to": "Maya Wilder", "text": "ping"})
    # Second dm reuses the same room instead of minting another.
    rooms = chats.list_chats()
    dms = [h for h, _ in rooms if h["kind"] == "dm"]
    assert len(dms) == 1
    _, messages = rooms[0]
    assert len([m for m in messages]) >= 1
    assert "Sent to" in out2 or "Started DM" in out2


def test_dm_excludes_user_participant():
    chats.chat_tool({"action": "dm", "to": "Maya Wilder", "text": "hi"})
    header = next(h for h, _ in chats.list_chats() if h["kind"] == "dm")
    assert set(header["participants"]) == {"Beatrix Winslow", "Maya Wilder"}


def test_post_by_prefix_and_unknown():
    chat_id = chats.start("Groom backlog", ["Maya Wilder"])
    prefix = chat_id[:10]
    assert "Posted" in chats.chat_tool(
        {"action": "post", "id": prefix, "text": "thoughts?"})
    assert "Error" in chats.chat_tool(
        {"action": "post", "id": "nope-nope", "text": "x"})


def test_outsider_post_joins_participants():
    chat_id = chats.start("Arch review", ["Maya Wilder"])
    chats.post(chat_id, "overheard — one concern", author="Anouk Beckett")
    header, _ = chats.load(chat_id)
    assert "Anouk Beckett" in header["participants"]


def test_user_posts_like_anyone():
    chat_id = chats.start("Sprint planning", ["Maya Wilder"])
    chats.post(chat_id, "Priority is the importer", author="user")
    _, messages = chats.load(chat_id)
    assert messages[-1]["from"] == "user"


def test_torn_line_is_skipped_not_fatal():
    chat_id = chats.start("Robustness", ["Maya Wilder"], text="first")
    with open(chats._path(chat_id), "a") as f:
        f.write('{"ts": 1, "from": "torn"\n')  # invalid json line
    chats.post(chat_id, "second", author="Maya Wilder")
    _, messages = chats.load(chat_id)
    assert [m["text"] for m in messages] == ["first", "second"]


def test_list_tool_output():
    chats.start("Planning", ["Maya Wilder"], text="kickoff")
    out = chats.chat_tool({"action": "list"})
    assert "Planning" in out and "kickoff" in out
