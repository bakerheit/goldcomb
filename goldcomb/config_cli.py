"""Machine-readable one-shot provider management CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .config import Config
from .presets import PRESETS
from .providers import PROVIDER_TYPES


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
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")))
        return 2
