import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from goldcomb.config import Config, validate_provider


def load_at(monkeypatch, tmp_path):
    monkeypatch.setenv("GOLDCOMB_CONFIG_DIR", str(tmp_path))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                "MOONSHOT_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return Config.load()


def test_redaction_key_source_and_permissions(monkeypatch, tmp_path):
    cfg = load_at(monkeypatch, tmp_path)
    cfg.add_provider("secret", "openai", "sk-private")
    listing = cfg.redacted()
    assert "api_key" not in json.dumps(listing)
    assert listing["providers"][0]["has_key"] is True
    assert listing["providers"][0]["key_source"] == "config"
    assert stat.S_IMODE(cfg.path.stat().st_mode) == 0o600


def test_env_key_source(monkeypatch, tmp_path):
    cfg = load_at(monkeypatch, tmp_path)
    cfg.add_provider("work", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")
    assert cfg.redacted()["providers"][0]["key_source"] == "env"


def test_rename_preserves_current_cache_and_model(monkeypatch, tmp_path):
    cfg = load_at(monkeypatch, tmp_path)
    cfg.add_provider("old", "openai", default_model="gpt-x")
    cfg.cache_models("old", ["gpt-x", "gpt-y"])
    cfg.update_provider("old", new_name="new", default_model="gpt-y")
    assert cfg.current == {"provider": "new", "model": "gpt-y"}
    assert cfg.models_cache["new"] == ["gpt-x", "gpt-y"]
    assert "old" not in cfg.providers


def test_merge_safe_stale_cache_writer(monkeypatch, tmp_path):
    first = load_at(monkeypatch, tmp_path)
    first.add_provider("one", "openai")
    stale = Config.load()
    fresh = Config.load()
    fresh.add_provider("two", "anthropic")
    stale.cache_models("one", ["m"])
    final = Config.load()
    assert set(final.providers) == {"one", "two"}
    assert final.models_cache["one"] == ["m"]


def test_concurrent_adds(monkeypatch, tmp_path):
    load_at(monkeypatch, tmp_path)

    def add(i):
        Config.load().add_provider(f"p{i}", "openai")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(20)))
    assert len(Config.load().providers) == 20


@pytest.mark.parametrize("name", ["UPPER", "under_score", "space name", ""])
def test_bad_names(name):
    with pytest.raises(ValueError):
        validate_provider(name, "openai")


def test_url_and_type_validation():
    with pytest.raises(ValueError):
        validate_provider("x", "unknown")
    with pytest.raises(ValueError):
        validate_provider("x", "openai-compatible")
    with pytest.raises(ValueError):
        validate_provider("x", "openai-compatible", "http://example.com/v1")
    validate_provider("x", "openai-compatible", "http://localhost:11434/v1")


def test_cli_key_stdin_never_returned(monkeypatch, tmp_path):
    load_at(monkeypatch, tmp_path)
    env = {**os.environ, "GOLDCOMB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "goldcomb", "config", "add", "--json",
         "--name", "safe", "--type", "openai", "--api-key-stdin"],
        input="top-secret\n", text=True, capture_output=True, env=env, check=True,
    )
    assert "top-secret" not in proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
    listed = subprocess.check_output(
        [sys.executable, "-m", "goldcomb", "config", "list", "--json"],
        text=True, env=env,
    )
    assert "top-secret" not in listed
    assert json.loads(listed)["providers"][0]["key_source"] == "config"


def test_revision_increments(monkeypatch, tmp_path):
    cfg = load_at(monkeypatch, tmp_path)
    initial = cfg.config_revision
    cfg.add_provider("one", "openai")
    assert cfg.config_revision > initial
    cfg.update_provider("one", default_model="gpt-new")
    assert cfg.config_revision > initial + 1
