"""Configuration: persistent providers, current model, and settings.

Config lives at ``$NEXAIS_CONFIG_DIR`` or ``~/.config/nexais/config.json`` and is
written with 0600 permissions since it holds API keys. On first run, providers
are auto-seeded from any recognized environment variables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_tokens": 4096,
    "temperature": None,
    "tools_enabled": True,
    "system_prompt": None,
    "stream": True,
    "render_markdown": True,
}

# env var -> (provider name, type)
ENV_PROVIDERS = [
    ("ANTHROPIC_API_KEY", "anthropic", "anthropic"),
    ("OPENAI_API_KEY", "openai", "openai"),
    ("GEMINI_API_KEY", "gemini", "gemini"),
    ("GOOGLE_API_KEY", "gemini", "gemini"),
    ("OPENROUTER_API_KEY", "openrouter", "openai-compatible"),
    ("GROQ_API_KEY", "groq", "openai-compatible"),
]

ENV_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}

# A sensible default model per provider type, for a freshly added provider.
DEFAULT_MODEL_BY_TYPE = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
    "gemini": "gemini-2.5-flash",
    "openai-compatible": "",
}


def config_dir() -> Path:
    env = os.environ.get("NEXAIS_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "nexais"


def config_path() -> Path:
    return config_dir() / "config.json"


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.providers: dict[str, dict[str, Any]] = data.get("providers", {})
        self.current: dict[str, str] = data.get("current", {})
        self.settings: dict[str, Any] = {**DEFAULT_SETTINGS, **data.get("settings", {})}

    # ---- persistence -------------------------------------------------------

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        cfg = cls(data, path)
        if not path.exists():
            cfg._seed_from_env()
            cfg.save()
        return cfg

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "providers": self.providers,
            "current": self.current,
            "settings": self.settings,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _seed_from_env(self) -> None:
        for env_var, name, ptype in ENV_PROVIDERS:
            key = os.environ.get(env_var)
            if not key or name in self.providers:
                continue
            entry: dict[str, Any] = {"type": ptype, "api_key": key}
            if name in ENV_BASE_URLS:
                entry["base_url"] = ENV_BASE_URLS[name]
            self.providers[name] = entry
            if not self.current:
                self.current = {
                    "provider": name,
                    "model": DEFAULT_MODEL_BY_TYPE.get(ptype, ""),
                }

    # ---- provider management ----------------------------------------------

    def add_provider(
        self,
        name: str,
        ptype: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"type": ptype}
        if api_key:
            entry["api_key"] = api_key
        if base_url:
            entry["base_url"] = base_url
        self.providers[name] = entry
        if not self.current:
            self.current = {"provider": name, "model": DEFAULT_MODEL_BY_TYPE.get(ptype, "")}
        self.save()

    def remove_provider(self, name: str) -> None:
        self.providers.pop(name, None)
        if self.current.get("provider") == name:
            self.current = {}
            for other in self.providers:
                p = self.providers[other]
                self.current = {
                    "provider": other,
                    "model": DEFAULT_MODEL_BY_TYPE.get(p.get("type", ""), ""),
                }
                break
        self.save()

    def set_provider_field(self, name: str, field: str, value: str) -> None:
        if name not in self.providers:
            raise KeyError(name)
        self.providers[name][field] = value
        self.save()

    def resolve_api_key(self, name: str) -> str | None:
        """Config key, falling back to a recognized env var for that provider."""
        entry = self.providers.get(name, {})
        if entry.get("api_key"):
            return entry["api_key"]
        ptype = entry.get("type")
        for env_var, _n, t in ENV_PROVIDERS:
            if t == ptype and os.environ.get(env_var):
                return os.environ[env_var]
        return None

    # ---- current selection -------------------------------------------------

    @property
    def current_provider(self) -> str | None:
        return self.current.get("provider")

    @property
    def current_model(self) -> str | None:
        return self.current.get("model")

    def use_provider(self, name: str, model: str | None = None) -> None:
        if name not in self.providers:
            raise KeyError(name)
        ptype = self.providers[name].get("type", "")
        self.current = {
            "provider": name,
            "model": model or DEFAULT_MODEL_BY_TYPE.get(ptype, ""),
        }
        self.save()

    def set_model(self, model: str) -> None:
        if not self.current.get("provider"):
            raise ValueError("No provider selected. Add one with /provider add.")
        self.current["model"] = model
        self.save()

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save()
