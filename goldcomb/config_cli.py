"""Machine-readable one-shot provider management CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from pathlib import Path

from . import oauth as oauth_mod
from .config import Config
from .presets import PRESETS
from .providers import PROVIDER_TYPES


def _pending_path(cfg: Config) -> Path:
    """Where the in-progress OAuth PKCE verifier is stashed (next to config)."""
    return cfg.path.parent / "oauth-pending.json"


def _default_base_url(ptype: str) -> str:
    """The endpoint an adapter uses when no base_url override is set — read off
    the provider class, so it's single-sourced with the code that calls it. The
    generic ``openai-compatible`` adapter has none of its own (a base_url is
    required), even though it inherits the class attribute from OpenAIProvider —
    so it's reported as empty, not the misleading OpenAI URL."""
    if ptype == "openai-compatible":
        return ""
    cls = PROVIDER_TYPES.get(ptype)
    return getattr(cls, "default_base_url", "") if cls else ""


def _presets_payload() -> dict[str, Any]:
    """The known-provider presets, for a GUI's "Add provider" picker. Each
    entry carries everything but the key, plus ``env_present`` so the app can
    offer "use the key already in your environment" without the user pasting
    it. Mirrors what the CLI /setup wizard fills in (goldcomb/presets.py).

    ``requires_base_url`` tells the app whether the field is mandatory (the
    generic openai-compatible adapter) or an optional override (anthropic /
    openai / gemini, which have a baked-in endpoint exposed as
    ``default_base_url``)."""
    return {
        "presets": [
            {
                "key": p.key,
                "label": p.label,
                "type": p.type,
                "default_model": p.default_model,
                "base_url": p.base_url or "",
                "default_base_url": _default_base_url(p.type),
                "requires_base_url": p.type == "openai-compatible",
                "env": p.env or "",
                "env_present": bool(p.env and os.environ.get(p.env)),
                "key_url": p.key_url or "",
                "needs_key": p.needs_key,
                "note": p.note,
            }
            for p in PRESETS
        ]
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goldcomb config")
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true", required=True)
    presets = sub.add_parser("presets")
    presets.add_argument("--json", action="store_true", required=True)
    for action in ("add", "update"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--json", action="store_true", required=True)
        cmd.add_argument("--name", required=True)
        cmd.add_argument("--type", dest="ptype", required=action == "add")
        cmd.add_argument("--base-url")
        cmd.add_argument("--default-model")
        cmd.add_argument("--api-key-stdin", action="store_true")
        cmd.add_argument("--current", action="store_true")
        if action == "update":
            cmd.add_argument("--new-name")
    # Claude subscription (OAuth) — the app drives the two-step browser flow.
    ourl = sub.add_parser("oauth-url")
    ourl.add_argument("--json", action="store_true", required=True)
    oex = sub.add_parser("oauth-exchange")
    oex.add_argument("--json", action="store_true", required=True)
    oex.add_argument("--name", required=True)
    oex.add_argument("--code-stdin", action="store_true", required=True)
    oex.add_argument("--current", action="store_true")
    olo = sub.add_parser("oauth-logout")
    olo.add_argument("--json", action="store_true", required=True)
    olo.add_argument("--name", required=True)
    return parser


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "presets":
            # No config needed — this is static, environment-derived data.
            print(json.dumps(_presets_payload(), separators=(",", ":")))
            return 0
        cfg = Config.load()
        if args.action == "list":
            result: dict[str, Any] = cfg.redacted()
        elif args.action == "oauth-url":
            # Step 1: mint PKCE + state, stash the verifier, hand back the URL.
            verifier, challenge = oauth_mod.generate_pkce()
            state = oauth_mod.new_state()
            oauth_mod.save_pending(_pending_path(cfg), state, verifier)
            print(json.dumps({"ok": True,
                              "url": oauth_mod.authorize_url(challenge, state),
                              "state": state}, separators=(",", ":")))
            return 0
        elif args.action == "oauth-exchange":
            # Step 2: exchange the pasted code using the stashed verifier, store
            # the credential on the (anthropic) provider, creating it if needed.
            pending = oauth_mod.load_pending(_pending_path(cfg))
            if pending is None:
                raise ValueError("no pending login — start with oauth-url first")
            _state, verifier = pending
            code = sys.stdin.read().strip()
            if not code:
                raise ValueError("no authorization code provided on stdin")
            creds = oauth_mod.exchange_code(code, verifier)
            if args.name not in cfg.providers:
                cfg.add_provider(args.name, "anthropic")
            cfg.set_oauth(args.name, creds.to_dict())
            if args.current:
                cfg.use_provider(args.name)
            oauth_mod.clear_pending(_pending_path(cfg))
            result = {"ok": True, **cfg.redacted()}
        elif args.action == "oauth-logout":
            cfg.clear_oauth(args.name)
            result = {"ok": True, **cfg.redacted()}
        else:
            key = sys.stdin.read().rstrip("\r\n") if args.api_key_stdin else None
            if args.action == "add":
                cfg.add_provider(args.name, args.ptype, key, args.base_url,
                                 args.default_model)
                if args.current:
                    cfg.use_provider(args.name)
            else:
                cfg.update_provider(args.name, new_name=args.new_name, ptype=args.ptype,
                                    api_key=key, base_url=args.base_url,
                                    default_model=args.default_model,
                                    make_current=args.current)
            result = {"ok": True, **cfg.redacted()}
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (ValueError, KeyError, OSError, oauth_mod.OAuthError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")))
        return 2
