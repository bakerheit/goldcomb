"""Persistent provider configuration with atomic, merge-safe writes."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_tokens": 4096, "temperature": None, "tools_enabled": True,
    "system_prompt": None, "stream": True, "render_markdown": True,
}
ENV_PROVIDERS = [
    ("ANTHROPIC_API_KEY", "anthropic", "anthropic"),
    ("OPENAI_API_KEY", "openai", "openai"),
    ("GEMINI_API_KEY", "gemini", "gemini"),
    ("GOOGLE_API_KEY", "gemini", "gemini"),
    ("OPENROUTER_API_KEY", "openrouter", "openai-compatible"),
    ("GROQ_API_KEY", "groq", "openai-compatible"),
    ("MOONSHOT_API_KEY", "kimi", "openai-compatible"),
]
ENV_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "kimi": "https://api.moonshot.ai/v1",
}
DEFAULT_MODEL_BY_TYPE = {
    "anthropic": "claude-opus-4-8", "openai": "gpt-4o",
    "gemini": "gemini-2.5-flash", "openai-compatible": "",
}
PROVIDER_TYPES = frozenset(DEFAULT_MODEL_BY_TYPE)
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def config_dir() -> Path:
    env = os.environ.get("GOLDCOMB_CONFIG_DIR") or os.environ.get("NEXAIS_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    new, legacy = base / "goldcomb", base / "nexais"
    if not new.exists() and legacy.exists():
        try:
            import shutil
            shutil.copytree(legacy, new)
        except OSError:
            return legacy
    return new


def config_path() -> Path:
    return config_dir() / "config.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with lock.open("a") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def validate_provider(name: str, ptype: str, base_url: str | None = None) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError("provider name must match [a-z0-9-]+")
    if ptype not in PROVIDER_TYPES:
        raise ValueError("type must be anthropic, openai, gemini, or openai-compatible")
    if ptype == "openai-compatible" and not base_url:
        raise ValueError("openai-compatible requires base_url")
    if base_url:
        parsed = urlparse(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"} or (
            parsed.hostname or "").endswith(".local")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("base_url must use HTTPS (HTTP is allowed only for local endpoints)")
        if not parsed.hostname:
            raise ValueError("base_url must be an absolute URL")


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.providers: dict[str, dict[str, Any]] = data.get("providers", {})
        self.current: dict[str, str] = data.get("current", {})
        self.settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        self.models_cache: dict[str, list[str]] = data.get("models_cache", {})
        self.config_revision = int(data.get("config_revision", 0))
        self._base = self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "providers": json.loads(json.dumps(self.providers)),
            "current": dict(self.current), "settings": dict(self.settings),
            "models_cache": json.loads(json.dumps(self.models_cache)),
            "config_revision": self.config_revision,
        }

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        exists = path.exists()
        cfg = cls(_read(path), path)
        if not exists:
            cfg._seed_from_env()
            cfg.save()
        return cfg

    def save(self) -> None:
        """Merge this instance's changed top-level entries into latest disk state."""
        with _locked(self.path):
            latest = Config(_read(self.path), self.path)
            for attr in ("providers", "settings", "models_cache"):
                old, new, disk = self._base[attr], getattr(self, attr), getattr(latest, attr)
                for key in old.keys() - new.keys():
                    disk.pop(key, None)
                for key, value in new.items():
                    if old.get(key) != value or key not in old:
                        disk[key] = value
            if self._base["current"] != self.current:
                latest.current = dict(self.current)
            latest.config_revision = max(latest.config_revision, self.config_revision) + 1
            payload = latest._snapshot()
            _atomic_write(self.path, payload)
            self.providers, self.current = latest.providers, latest.current
            self.settings, self.models_cache = latest.settings, latest.models_cache
            self.config_revision = latest.config_revision
            self._base = self._snapshot()

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
                self.current = {"provider": name, "model": DEFAULT_MODEL_BY_TYPE[ptype]}

    def add_provider(self, name: str, ptype: str, api_key: str | None = None,
                     base_url: str | None = None, default_model: str | None = None) -> None:
        validate_provider(name, ptype, base_url)
        with _locked(self.path):
            data = _read(self.path)
            if name in data.get("providers", {}):
                raise ValueError(f"provider already exists: {name}")
            latest = Config(data, self.path)
            entry: dict[str, Any] = {
                "type": ptype,
                "default_model": default_model if default_model is not None
                else DEFAULT_MODEL_BY_TYPE[ptype],
            }
            if api_key:
                entry["api_key"] = api_key
            if base_url:
                entry["base_url"] = base_url
            latest.providers[name] = entry
            if not latest.current:
                latest.current = {"provider": name, "model": entry["default_model"]}
            latest.config_revision += 1
            _atomic_write(self.path, latest._snapshot())
        self.__init__(_read(self.path), self.path)

    def update_provider(self, name: str, *, new_name: str | None = None,
                        ptype: str | None = None, api_key: str | None = None,
                        base_url: str | None = None, default_model: str | None = None,
                        make_current: bool | None = None) -> None:
        with _locked(self.path):
            latest = Config(_read(self.path), self.path)
            if name not in latest.providers:
                raise ValueError(f"unknown provider: {name}")
            target = new_name or name
            entry = dict(latest.providers[name])
            final_type = ptype or entry.get("type", "")
            final_url = base_url if base_url is not None else entry.get("base_url")
            validate_provider(target, final_type, final_url)
            if target != name and target in latest.providers:
                raise ValueError(f"provider already exists: {target}")
            entry["type"] = final_type
            if api_key:
                entry["api_key"] = api_key
            if base_url is not None:
                if base_url:
                    entry["base_url"] = base_url
                else:
                    entry.pop("base_url", None)
            if default_model is not None:
                entry["default_model"] = default_model
            entry.setdefault("default_model", DEFAULT_MODEL_BY_TYPE[final_type])
            if target != name:
                latest.providers.pop(name)
                latest.models_cache[target] = latest.models_cache.pop(name, [])
                if latest.current.get("provider") == name:
                    latest.current["provider"] = target
            latest.providers[target] = entry
            if make_current:
                latest.current = {"provider": target, "model": entry["default_model"]}
            elif latest.current.get("provider") == target and default_model is not None:
                latest.current["model"] = default_model
            if latest.current.get("provider") not in latest.providers:
                raise ValueError("current provider must remain configured")
            latest.config_revision += 1
            _atomic_write(self.path, latest._snapshot())
        self.__init__(_read(self.path), self.path)

    def remove_provider(self, name: str) -> None:
        self.providers.pop(name, None)
        self.models_cache.pop(name, None)
        if self.current.get("provider") == name:
            self.current = {}
            for other, entry in self.providers.items():
                self.current = {"provider": other, "model": entry.get(
                    "default_model", DEFAULT_MODEL_BY_TYPE.get(entry.get("type", ""), ""))}
                break
        self.save()

    def set_provider_field(self, name: str, field: str, value: str) -> None:
        if name not in self.providers:
            raise KeyError(name)
        self.providers[name][field] = value
        self.save()

    def key_source(self, name: str) -> str:
        entry = self.providers.get(name, {})
        if entry.get("oauth", {}).get("refresh_token"):
            return "subscription"  # Claude Pro/Max via OAuth (see oauth.py)
        if entry.get("api_key"):
            return "config"
        for env_var, _name, ptype in ENV_PROVIDERS:
            if ptype == entry.get("type") and os.environ.get(env_var):
                return "env"
        return "none"

    def resolve_api_key(self, name: str) -> str | None:
        entry = self.providers.get(name, {})
        if entry.get("api_key"):
            return entry["api_key"]
        for env_var, _name, ptype in ENV_PROVIDERS:
            if ptype == entry.get("type") and os.environ.get(env_var):
                return os.environ[env_var]
        return None

    # -- OAuth (Claude subscription) credentials --------------------------

    def oauth_credentials(self, name: str) -> dict | None:
        """The stored OAuth credential for a provider (access/refresh/expires),
        or None. See goldcomb/oauth.py."""
        oauth = self.providers.get(name, {}).get("oauth")
        return oauth if isinstance(oauth, dict) and oauth.get("refresh_token") else None

    def set_oauth(self, name: str, creds: dict) -> None:
        """Store (or replace) a provider's OAuth credential and persist. An
        OAuth-authed provider needs no api_key; leave any existing one alone."""
        if name not in self.providers:
            raise KeyError(name)
        self.providers[name]["oauth"] = {
            "access_token": creds.get("access_token", ""),
            "refresh_token": creds.get("refresh_token", ""),
            "expires_at": creds.get("expires_at", 0),
        }
        self.save()

    def clear_oauth(self, name: str) -> None:
        if name in self.providers:
            self.providers[name].pop("oauth", None)
            self.save()

    def redacted(self) -> dict[str, Any]:
        providers = [
            {
                "name": name,
                "type": entry.get("type", ""),
                "base_url": entry.get("base_url", ""),
                "default_model": entry.get(
                    "default_model",
                    DEFAULT_MODEL_BY_TYPE.get(entry.get("type", ""), ""),
                ),
                "has_key": self.key_source(name) != "none",
                "key_source": self.key_source(name),
            }
            for name, entry in sorted(self.providers.items())
        ]
        return {
            "config_revision": self.config_revision,
            "current": self.current,
            "providers": providers,
        }

    @property
    def current_provider(self) -> str | None:
        return self.current.get("provider")

    @property
    def current_model(self) -> str | None:
        return self.current.get("model")

    def use_provider(self, name: str, model: str | None = None) -> None:
        if name not in self.providers:
            raise KeyError(name)
        entry = self.providers[name]
        self.current = {"provider": name, "model": model or entry.get(
            "default_model", DEFAULT_MODEL_BY_TYPE.get(entry.get("type", ""), ""))}
        self.save()

    def set_model(self, model: str) -> None:
        if not self.current.get("provider"):
            raise ValueError("No provider selected. Add one with /provider add.")
        self.current["model"] = model
        self.save()

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save()

    def cache_models(self, provider: str, models: list[str]) -> None:
        self.models_cache[provider] = list(models)
        self.save()

    def models_for(self, provider: str | None) -> list[str]:
        return list(self.models_cache.get(provider or "", []))
