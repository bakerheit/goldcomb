"""`goldcomb config presets --json` — the known-provider list the macOS app's
"Add provider" picker consumes so a user only pastes a key.

The app decodes this exact shape (Swift ProviderPreset), so the field set is a
contract: every preset must round-trip with the keys the app expects, and the
env-var presence must reflect the real environment.
"""

import json

from goldcomb import config_cli
from goldcomb.presets import PRESETS

_EXPECTED_KEYS = {
    "key", "label", "type", "default_model", "base_url",
    "default_base_url", "requires_base_url",
    "env", "env_present", "key_url", "needs_key", "note",
}


def _run_presets(capsys) -> dict:
    rc = config_cli.run(["presets", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out)


def test_presets_command_emits_every_preset(capsys):
    payload = _run_presets(capsys)
    assert [p["key"] for p in payload["presets"]] == [p.key for p in PRESETS]


def test_every_preset_has_the_fields_the_app_decodes(capsys):
    payload = _run_presets(capsys)
    for p in payload["presets"]:
        assert set(p.keys()) == _EXPECTED_KEYS, p["key"]


def test_known_providers_are_present(capsys):
    payload = _run_presets(capsys)
    keys = {p["key"] for p in payload["presets"]}
    # The headline hosted providers plus a couple of openai-compatible ones.
    assert {"anthropic", "openai", "gemini", "kimi"} <= keys


def test_hosted_presets_carry_type_and_default_model(capsys):
    payload = _run_presets(capsys)
    by_key = {p["key"]: p for p in payload["presets"]}
    assert by_key["anthropic"]["type"] == "anthropic"
    assert by_key["anthropic"]["default_model"]        # non-empty
    assert by_key["gemini"]["type"] == "gemini"
    # openai-compatible presets prefill the base URL the app would need.
    assert by_key["kimi"]["base_url"].startswith("https://")


def test_base_url_requirement_matches_the_type(capsys):
    """First-party types have a baked-in endpoint (override optional);
    openai-compatible requires one and has no default of its own."""
    payload = _run_presets(capsys)
    by_key = {p["key"]: p for p in payload["presets"]}
    # Built-in endpoint: not required, and the default is reported for a hint.
    assert by_key["anthropic"]["requires_base_url"] is False
    assert by_key["anthropic"]["default_base_url"] == "https://api.anthropic.com"
    assert by_key["gemini"]["default_base_url"].startswith("https://")
    # openai-compatible: required, and no misleading inherited default.
    assert by_key["kimi"]["requires_base_url"] is True
    assert by_key["kimi"]["default_base_url"] == ""


def test_env_present_reflects_the_environment(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload = _run_presets(capsys)
    by_key = {p["key"]: p for p in payload["presets"]}
    assert by_key["anthropic"]["env_present"] is True
    assert by_key["openai"]["env_present"] is False


def test_local_presets_do_not_need_a_key(capsys):
    payload = _run_presets(capsys)
    by_key = {p["key"]: p for p in payload["presets"]}
    # Ollama / LM Studio run locally — the app should not force a key.
    assert by_key["ollama"]["needs_key"] is False


def test_presets_command_needs_no_config(capsys, tmp_path, monkeypatch):
    """It's static, environment-derived data — must work before any provider
    is configured (that's the whole point: the first-run add flow)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no config file here
    payload = _run_presets(capsys)
    assert payload["presets"]
