"""Chat attachments — Phase A (NEXA-74 format contract, NEXA-75 agent side).

Attachments are file *references*, not inline payloads: the bytes are copied
into a per-room sidecar and the message line carries
``attachments:[{name,path,mime,size}]``. The invariants that matter — copy
before append (no dangling reference), old readers tolerate the extra key,
project-relative paths (no absolute user path leaks into a digest), and the
agent-facing wording that tells a text-only model it cannot see an image.
"""

import json
import os

import pytest

from goldcomb import chats, scrum


@pytest.fixture(autouse=True)
def _in_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scrum.set_agent("Ada")
    yield


def _src(name: str, data: bytes = b"x") -> str:
    from pathlib import Path
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    Path(name).write_bytes(data)
    return name


# -- storage & format --------------------------------------------------------

def test_attachment_is_copied_into_room_sidecar():
    _src("out/diff.patch", b"--- a\n+++ b\n")
    cid = chats.start("review", ["Quill"], text="see this", attachments=["out/diff.patch"])
    _, msgs = chats.load(cid)
    att = msgs[0]["attachments"][0]
    assert att["name"] == "diff.patch"
    assert att["size"] == len(b"--- a\n+++ b\n")
    # path is project-relative, inside the room's sidecar, and the file exists.
    assert att["path"].startswith(f".ai/chats/attachments/{cid}/")
    assert not os.path.isabs(att["path"])
    assert os.path.isfile(att["path"])


def test_original_source_is_left_in_place():
    _src("keep.txt", b"hi")
    cid = chats.start("r", ["Q"], text="x", attachments=["keep.txt"])
    assert os.path.isfile("keep.txt")  # copy, not move


def test_message_without_attachments_has_no_key():
    """Old readers tolerate the extra key, but we don't emit it needlessly."""
    cid = chats.start("r", ["Q"], text="plain")
    _, msgs = chats.load(cid)
    assert "attachments" not in msgs[0]


def test_mime_is_detected():
    _src("shot.png", b"\x89PNG")
    cid = chats.start("r", ["Q"], text="", attachments=["shot.png"])
    _, msgs = chats.load(cid)
    assert msgs[0]["attachments"][0]["mime"] == "image/png"


# -- the copy-before-append invariant ---------------------------------------

def test_missing_source_raises_and_writes_nothing():
    cid = chats.start("r", ["Q"], text="hello")
    _, before = chats.load(cid)
    with pytest.raises(ValueError, match="not found"):
        chats.post(cid, "here", attachments=["ghost.bin"])
    _, after = chats.load(cid)
    assert len(after) == len(before)  # the failed post left no dangling line


def test_oversize_source_raises(monkeypatch):
    monkeypatch.setattr(chats, "MAX_ATTACH_BYTES", 4)
    _src("big.bin", b"12345")
    cid = chats.start("r", ["Q"], text="x")
    with pytest.raises(ValueError, match="too large"):
        chats.post(cid, "big one", attachments=["big.bin"])


# -- agent-facing wording (the digest / read view) ---------------------------

def test_non_image_points_at_read_file():
    _src("log.txt", b"trace")
    cid = chats.start("r", ["Q"], text="", attachments=["log.txt"])
    _, msgs = chats.load(cid)
    line = chats._attach_line(msgs[0]["attachments"][0])
    assert "read_file" in line and "log.txt" in line


def test_image_says_it_cannot_be_seen():
    """A text-only model must not hallucinate having viewed the image."""
    _src("ui.png", b"\x89PNG")
    cid = chats.start("r", ["Q"], text="", attachments=["ui.png"])
    _, msgs = chats.load(cid)
    line = chats._attach_line(msgs[0]["attachments"][0])
    assert "cannot view images" in line


# -- the tool surface --------------------------------------------------------

def test_post_action_accepts_attachments_only():
    """A file with no text is a valid post."""
    _src("art.txt", b"data")
    cid = chats.start("r", ["Q"], text="kick off")
    out = chats.chat_tool({"action": "post", "id": cid, "attachments": ["art.txt"]})
    assert "Posted" in out and "attachment" in out
    _, msgs = chats.load(cid)
    assert msgs[-1]["attachments"][0]["name"] == "art.txt"


def test_post_action_rejects_empty():
    cid = chats.start("r", ["Q"], text="x")
    out = chats.chat_tool({"action": "post", "id": cid})
    assert out.startswith("Error")


def test_tool_reports_bad_attachment_as_error_not_crash():
    cid = chats.start("r", ["Q"], text="x")
    out = chats.chat_tool({"action": "post", "id": cid, "text": "hi",
                           "attachments": ["nope.bin"]})
    assert out.startswith("Error") and "not found" in out


def test_lone_string_attachment_is_accepted():
    """Models often pass a bare path instead of a list."""
    _src("one.txt", b"z")
    cid = chats.start("r", ["Q"], text="x")
    chats.chat_tool({"action": "post", "id": cid, "text": "f",
                     "attachments": "one.txt"})
    _, msgs = chats.load(cid)
    assert msgs[-1]["attachments"][0]["name"] == "one.txt"
