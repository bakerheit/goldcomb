"""Tests for the /models chat-only de-noise filter."""

from goldcomb.cli import chat_models_only


def test_keeps_chat_models():
    keep = ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini",
            "gpt-5", "gpt-5-codex", "gpt-4o-search-preview", "chatgpt-4o-latest"]
    assert chat_models_only(keep) == keep


def test_drops_non_chat_families():
    raw = [
        "gpt-4o",                       # keep
        "text-embedding-3-large",       # embedding
        "text-embedding-ada-002",       # embedding
        "whisper-1",                    # audio in
        "tts-1", "gpt-4o-mini-tts",     # audio out
        "dall-e-3", "gpt-image-1",      # image
        "gpt-4o-realtime-preview",      # realtime
        "gpt-audio",                    # audio
        "babbage-002", "davinci-002",   # legacy completion
        "gpt-3.5-turbo-instruct",       # completion
        "computer-use-preview",         # specialized
        "omni-moderation-latest",       # moderation
        "gpt-4o-transcribe",            # transcription
        "sora-2", "sora-2-pro",         # video
    ]
    assert chat_models_only(raw) == ["gpt-4o"]


def test_empty_input():
    assert chat_models_only([]) == []
