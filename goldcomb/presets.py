"""Known-provider presets for the guided setup wizard.

Each preset fills in the fiddly bits (type, base_url, a sane default model, the
env-var name, and where to get a key) so the user only has to paste a key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Preset:
    key: str  # short id, also the default provider name
    label: str  # human-facing menu label
    type: str  # provider type
    default_model: str = ""
    base_url: str | None = None
    env: str | None = None  # env var checked for an existing key
    key_url: str | None = None  # where to get a key
    needs_key: bool = True
    note: str = ""


# Ordered for the setup menu.
PRESETS: list[Preset] = [
    Preset(
        key="anthropic",
        label="Anthropic — Claude",
        type="anthropic",
        default_model="claude-opus-4-8",
        env="ANTHROPIC_API_KEY",
        key_url="https://console.anthropic.com/settings/keys",
    ),
    Preset(
        key="openai",
        label="OpenAI — GPT",
        type="openai",
        default_model="gpt-4o",
        env="OPENAI_API_KEY",
        key_url="https://platform.openai.com/api-keys",
    ),
    Preset(
        key="gemini",
        label="Google — Gemini",
        type="gemini",
        default_model="gemini-2.5-flash",
        env="GEMINI_API_KEY",
        key_url="https://aistudio.google.com/apikey",
    ),
    Preset(
        key="openrouter",
        label="OpenRouter — hundreds of models via one key",
        type="openai-compatible",
        base_url="https://openrouter.ai/api/v1",
        default_model="",
        env="OPENROUTER_API_KEY",
        key_url="https://openrouter.ai/keys",
        note="Pick a model after setup with /models.",
    ),
    Preset(
        key="groq",
        label="Groq — very fast open models",
        type="openai-compatible",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        env="GROQ_API_KEY",
        key_url="https://console.groq.com/keys",
    ),
    Preset(
        key="deepseek",
        label="DeepSeek",
        type="openai-compatible",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        env="DEEPSEEK_API_KEY",
        key_url="https://platform.deepseek.com/api_keys",
    ),
    Preset(
        key="kimi",
        label="Moonshot AI — Kimi",
        type="openai-compatible",
        base_url="https://api.moonshot.ai/v1",
        default_model="kimi-k3",
        env="MOONSHOT_API_KEY",
        key_url="https://platform.moonshot.ai/console/api-keys",
    ),
    Preset(
        key="mistral",
        label="Mistral",
        type="openai-compatible",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-large-latest",
        env="MISTRAL_API_KEY",
        key_url="https://console.mistral.ai/api-keys",
    ),
    Preset(
        key="together",
        label="Together AI",
        type="openai-compatible",
        base_url="https://api.together.xyz/v1",
        default_model="",
        env="TOGETHER_API_KEY",
        key_url="https://api.together.ai/settings/api-keys",
    ),
    Preset(
        key="ollama",
        label="Ollama — local models (no key)",
        type="openai-compatible",
        base_url="http://localhost:11434/v1",
        default_model="llama3.2",
        needs_key=False,
        note="Requires Ollama running locally (ollama serve).",
    ),
    Preset(
        key="lmstudio",
        label="LM Studio — local models (no key)",
        type="openai-compatible",
        base_url="http://localhost:1234/v1",
        default_model="",
        needs_key=False,
        note="Start LM Studio's local server first.",
    ),
]

PRESETS_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}
