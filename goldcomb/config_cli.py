"""Machine-readable one-shot provider management CLI."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import Config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goldcomb config")
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true", required=True)
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
