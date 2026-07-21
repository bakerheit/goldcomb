"""Claude subscription (Pro/Max) auth via OAuth.

This lets a user drive goldcomb's Anthropic provider with their Claude
subscription instead of a pay-per-token API key. It uses the same OAuth
mechanism the official Claude Code client uses: an Authorization-Code + PKCE
flow that yields a short-lived access token plus a refresh token, sent as
``Authorization: Bearer <token>`` with the ``anthropic-beta: oauth-2025-04-20``
header (no ``x-api-key``).

IMPORTANT CAVEATS (surface these to the user, don't bury them):
- This uses Claude Code's public OAuth client id. Using a Claude *subscription*
  from a third-party tool is a grey area under Anthropic's terms; Anthropic can
  restrict subscription tokens to its own clients, and this may stop working.
- The endpoints, client id, and scopes below are the community-known Claude
  Code values, not an officially documented third-party integration — they may
  change without notice.
- Access tokens are short-lived; the caller must refresh before expiry
  (see ``needs_refresh`` / ``refresh_tokens``).

The pure helpers (PKCE, the authorize URL, expiry math) are unit-tested; the two
functions that call Anthropic's live token endpoint are not (they need a real
account and network), so treat them as best-effort until verified end-to-end.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

#: Claude Code's public OAuth client id (community-known; not a secret).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
#: Console callback that displays the code for the user to paste back.
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
#: `user:inference` is what lets the token call /v1/messages on the sub.
SCOPES = "org:create_api_key user:profile user:inference"
#: Header that makes /v1/messages accept a Bearer subscription token.
OAUTH_BETA = "oauth-2025-04-20"

#: Refresh this many seconds before the token actually expires, so a request
#: never goes out on a token about to lapse mid-flight.
_REFRESH_SKEW_S = 120


def _b64url(data: bytes) -> str:
    """URL-safe base64 with padding stripped (RFC 7636 / OAuth PKCE)."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def generate_pkce() -> tuple[str, str]:
    """A PKCE (verifier, challenge) pair. The verifier is kept secret until the
    token exchange; the challenge goes in the authorize URL."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_state() -> str:
    """A random anti-CSRF state, echoed back on the redirect."""
    return _b64url(secrets.token_bytes(32))


def authorize_url(challenge: str, state: str) -> str:
    """The URL the user opens to grant access. The console redirect shows a
    ``<code>#<state>`` string for them to paste back (see ``split_pasted``)."""
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def split_pasted(pasted: str) -> tuple[str, str]:
    """The console callback shows ``<code>#<state>``; the user may paste the
    whole thing. Return (code, state); state is "" if they pasted just a code."""
    text = pasted.strip()
    if "#" in text:
        code, state = text.split("#", 1)
        return code.strip(), state.strip()
    return text, ""


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    #: Absolute epoch seconds when the access token expires.
    expires_at: float

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        return cls(
            access_token=str(d.get("access_token") or ""),
            refresh_token=str(d.get("refresh_token") or ""),
            expires_at=float(d.get("expires_at") or 0),
        )


def _creds_from_response(payload: dict, now: float | None = None) -> Credentials:
    """Normalize a token-endpoint response into Credentials. ``expires_in`` is
    relative seconds; store it as an absolute time so refresh checks are simple.
    A refresh response may omit ``refresh_token`` — the caller keeps the old one
    in that case (handled in refresh_tokens)."""
    now = time.time() if now is None else now
    expires_in = float(payload.get("expires_in") or 0)
    return Credentials(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=now + expires_in,
    )


def needs_refresh(creds: Credentials, now: float | None = None) -> bool:
    """True when the access token is expired or about to (within the skew)."""
    now = time.time() if now is None else now
    return creds.expires_at - _REFRESH_SKEW_S <= now


# -- pending PKCE (bridges the two-step GUI flow) -----------------------------
#
# The browser step and the code-paste step are separate commands (the app can't
# block on a paste). The PKCE verifier minted for the URL must survive until the
# exchange, so it's stashed in the config dir keyed by nothing (single pending
# login at a time — starting a new one overwrites the old).

def save_pending(path: Path, state: str, verifier: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state, "verifier": verifier}))
    tmp.replace(path)


def load_pending(path: Path) -> tuple[str, str] | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    state, verifier = d.get("state"), d.get("verifier")
    return (str(state), str(verifier)) if state and verifier else None


def clear_pending(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# -- live calls (not unit-tested; need a real account + network) --------------

class OAuthError(RuntimeError):
    """A token-endpoint call failed."""


def exchange_code(code: str, verifier: str, state: str = "") -> Credentials:
    """Exchange an authorization code for tokens. ``code`` may be the raw code
    or the pasted ``<code>#<state>`` (split here)."""
    parsed_code, parsed_state = split_pasted(code)
    body = {
        "grant_type": "authorization_code",
        "code": parsed_code,
        "state": parsed_state or state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    return _post_token(body)


def refresh_tokens(refresh_token: str) -> Credentials:
    """Exchange a refresh token for a fresh access token. The response may not
    include a new refresh token; if so, carry the old one forward."""
    creds = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })
    if not creds.refresh_token:
        creds.refresh_token = refresh_token
    return creds


def _post_token(body: dict) -> Credentials:
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(TOKEN_URL, json=body,
                            headers={"content-type": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError(f"network error talking to Anthropic OAuth: {e}") from e
    if r.status_code >= 400:
        detail = r.text[:300]
        raise OAuthError(f"OAuth token request failed ({r.status_code}): {detail}")
    try:
        payload = r.json()
    except ValueError as e:
        raise OAuthError(f"OAuth response was not JSON: {e}") from e
    creds = _creds_from_response(payload)
    if not creds.access_token:
        raise OAuthError("OAuth response contained no access_token")
    return creds
