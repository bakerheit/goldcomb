"""Claude-subscription OAuth (goldcomb/oauth.py) + the provider/config wiring.

The live token-endpoint calls (exchange/refresh) need a real account + network
and aren't exercised here — only the pure pieces (PKCE, the authorize URL,
expiry math, pending storage) and how the credential drives the request headers.
"""

import time
from urllib.parse import parse_qs, urlparse

from goldcomb import oauth
from goldcomb.config import Config
from goldcomb.providers.anthropic import AnthropicProvider


# -- PKCE ---------------------------------------------------------------------

def test_pkce_pair_is_urlsafe_and_unpadded():
    verifier, challenge = oauth.generate_pkce()
    assert 43 <= len(verifier) <= 128
    for s in (verifier, challenge):
        assert "=" not in s and "+" not in s and "/" not in s


def test_pkce_is_random_each_time():
    assert oauth.generate_pkce()[0] != oauth.generate_pkce()[0]


# -- authorize URL ------------------------------------------------------------

def test_authorize_url_carries_the_oauth_params():
    url = oauth.authorize_url("CHAL", "STATE")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == [oauth.CLIENT_ID]
    assert q["code_challenge"] == ["CHAL"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["STATE"]
    assert q["response_type"] == ["code"]
    assert "user:inference" in q["scope"][0]


def test_split_pasted_separates_code_and_state():
    assert oauth.split_pasted("theCode#theState") == ("theCode", "theState")
    assert oauth.split_pasted("  bare-code  ") == ("bare-code", "")


# -- expiry -------------------------------------------------------------------

def test_needs_refresh_within_the_skew():
    now = 1_000_000.0
    fresh = oauth.Credentials("a", "r", expires_at=now + 3600)
    stale = oauth.Credentials("a", "r", expires_at=now + 30)  # inside skew
    expired = oauth.Credentials("a", "r", expires_at=now - 5)
    assert not oauth.needs_refresh(fresh, now=now)
    assert oauth.needs_refresh(stale, now=now)
    assert oauth.needs_refresh(expired, now=now)


def test_creds_round_trip_dict():
    c = oauth.Credentials("acc", "ref", expires_at=123.0)
    assert oauth.Credentials.from_dict(c.to_dict()) == c


def test_creds_from_response_makes_expiry_absolute():
    c = oauth._creds_from_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
        now=1000.0)
    assert c.expires_at == 4600.0


# -- pending PKCE stash -------------------------------------------------------

def test_pending_round_trip(tmp_path):
    p = tmp_path / "oauth-pending.json"
    assert oauth.load_pending(p) is None
    oauth.save_pending(p, "st", "vf")
    assert oauth.load_pending(p) == ("st", "vf")
    oauth.clear_pending(p)
    assert oauth.load_pending(p) is None


# -- provider headers: Bearer vs x-api-key ------------------------------------

def test_provider_uses_bearer_and_beta_for_oauth():
    p = AnthropicProvider("claude", {"type": "anthropic", "oauth_token": "TOK"})
    h = p._headers()
    assert h["authorization"] == "Bearer TOK"
    assert h["anthropic-beta"] == oauth.OAUTH_BETA
    assert "x-api-key" not in h  # mutually exclusive with Bearer


def test_provider_uses_api_key_without_oauth():
    p = AnthropicProvider("anthropic", {"type": "anthropic", "api_key": "sk-x"})
    h = p._headers()
    assert h["x-api-key"] == "sk-x"
    assert "authorization" not in h


# -- config storage -----------------------------------------------------------

def _cfg(tmp_path):
    path = tmp_path / "config.json"
    return Config({"providers": {"claude": {"type": "anthropic"}},
                   "current": {"provider": "claude"}, "settings": {}}, path)


def test_set_and_read_oauth_credential(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.oauth_credentials("claude") is None
    cfg.set_oauth("claude", {"access_token": "a", "refresh_token": "r",
                             "expires_at": time.time() + 3600})
    got = cfg.oauth_credentials("claude")
    assert got["refresh_token"] == "r"
    # A subscription provider reports its key source distinctly.
    assert cfg.key_source("claude") == "subscription"


def test_redacted_does_not_leak_oauth_tokens(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.set_oauth("claude", {"access_token": "SECRET", "refresh_token": "SECRET2",
                             "expires_at": 0})
    blob = str(cfg.redacted())
    assert "SECRET" not in blob and "SECRET2" not in blob
    entry = next(p for p in cfg.redacted()["providers"] if p["name"] == "claude")
    assert entry["key_source"] == "subscription" and entry["has_key"] is True


def test_clear_oauth(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.set_oauth("claude", {"access_token": "a", "refresh_token": "r", "expires_at": 0})
    cfg.clear_oauth("claude")
    assert cfg.oauth_credentials("claude") is None


# -- refresh-on-use (App._provider_entry) -------------------------------------

def test_provider_entry_refreshes_expiring_token(tmp_path, monkeypatch):
    from goldcomb.cli import App

    cfg = _cfg(tmp_path)
    cfg.set_oauth("claude", {"access_token": "old", "refresh_token": "R",
                             "expires_at": time.time() - 10})  # expired

    refreshed = oauth.Credentials("NEW", "R2", expires_at=time.time() + 3600)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda rt: refreshed)

    app = App.__new__(App)
    app.cfg = cfg
    entry = app._provider_entry("claude")

    assert entry["oauth_token"] == "NEW"       # the refreshed access token
    assert "api_key" not in entry              # Bearer path, no key
    # The refresh was persisted (so the next process doesn't re-refresh).
    assert cfg.oauth_credentials("claude")["access_token"] == "NEW"


def test_provider_entry_keeps_fresh_token_without_refresh(tmp_path, monkeypatch):
    from goldcomb.cli import App

    cfg = _cfg(tmp_path)
    cfg.set_oauth("claude", {"access_token": "good", "refresh_token": "R",
                             "expires_at": time.time() + 3600})

    def _boom(_rt):
        raise AssertionError("must not refresh a fresh token")
    monkeypatch.setattr(oauth, "refresh_tokens", _boom)

    app = App.__new__(App)
    app.cfg = cfg
    assert app._provider_entry("claude")["oauth_token"] == "good"


# -- config_cli OAuth commands (app-driven) -----------------------------------

def test_config_cli_oauth_url_and_exchange(tmp_path, monkeypatch, capsys):
    import json as _json

    from goldcomb import config_cli

    monkeypatch.setenv("HOME", str(tmp_path))  # isolate the config location

    # Step 1: get the URL (also stashes the PKCE verifier).
    assert config_cli.run(["oauth-url", "--json"]) == 0
    step1 = _json.loads(capsys.readouterr().out)
    assert step1["ok"] and step1["url"].startswith(oauth.AUTHORIZE_URL)

    # Step 2: exchange the pasted code (live call mocked).
    monkeypatch.setattr(oauth, "exchange_code",
                        lambda code, verifier, state="":
                        oauth.Credentials("acc", "ref", expires_at=time.time() + 3600))
    monkeypatch.setattr("sys.stdin", _Stdin("thecode#thestate"))
    rc = config_cli.run(["oauth-exchange", "--json", "--name", "claude",
                        "--code-stdin", "--current"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["ok"]
    entry = next(p for p in out["providers"] if p["name"] == "claude")
    assert entry["key_source"] == "subscription"


def test_config_cli_oauth_exchange_without_pending_errors(tmp_path, monkeypatch, capsys):
    import json as _json

    from goldcomb import config_cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", _Stdin("code"))
    rc = config_cli.run(["oauth-exchange", "--json", "--name", "claude", "--code-stdin"])
    assert rc == 2
    assert _json.loads(capsys.readouterr().out)["ok"] is False


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text
