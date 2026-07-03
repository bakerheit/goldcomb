# nexais

A **Claude-Code-style terminal AI agent that works with any provider.** Configure
providers and switch models with slash commands — no code changes, no restarts.

Talk to Anthropic Claude, OpenAI, Google Gemini, or any OpenAI-compatible endpoint
(OpenRouter, Groq, Together, Ollama, LM Studio, vLLM, …) from one CLI, with streaming
responses and built-in file/shell tools so the model can actually do work.

```
┌ nexais — multi-provider AI agent for the terminal ┐
│ Type /help for commands, /exit to quit.           │
└───────────────────────────────────────────────────┘
Using anthropic / claude-opus-4-8

› /provider add openai openai
API key for 'openai': ****
Added provider 'openai' (openai).

› /use openai gpt-4o
Using openai / gpt-4o

› read pyproject.toml and tell me the deps
openai (gpt-4o)
⚙ read_file(pyproject.toml)
The dependencies are httpx, rich, and prompt_toolkit…
```

## Install

Requires Python 3.10+.

```bash
pip install -e .          # from this directory
# or, without installing:
python -m nexais
```

This installs a `nexais` command. Dependencies: `httpx`, `rich`, `prompt_toolkit`.

## Quick start

```bash
nexais                    # interactive session
nexais -p "what is 2+2"   # one-shot answer
echo "explain this" | nexais   # piped input
```

On first run, nexais auto-detects any of these environment variables and configures a
provider for you: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`. Otherwise, add one with a slash
command.

## Configure providers with a slash command

```
/provider add <name> <type> [api_key] [base_url]
```

`type` is one of `anthropic`, `openai`, `gemini`, `openai-compatible` (aliases:
`claude`, `gpt`, `google`, `openrouter`, `groq`, `ollama`, …). If you omit the key it's
prompted for (hidden). `openai-compatible` requires a `base_url`.

```
/provider add claude   anthropic
/provider add oai      openai
/provider add gflash   gemini
/provider add local    openai-compatible  ""  http://localhost:11434/v1   # Ollama
/provider add router   openrouter                                          # preset base_url
```

Other provider commands:

```
/provider list                       show configured providers (→ marks active)
/provider set <name> <field> <value> edit api_key / base_url
/provider remove <name>              delete a provider
```

## Switch models with a slash command

```
/use <provider> [model]     switch active provider (and optionally model)
/model <name>               set the model on the current provider
/model list                 known models for this provider type
/models                     query the provider's API for its live model list
```

```
› /use claude claude-sonnet-5
Using claude / claude-sonnet-5
› /model claude-opus-4-8
Model set to claude-opus-4-8
```

## Built-in tools (agentic mode)

When tools are on (default), the model can call:

| tool | what it does | needs confirmation |
|------|--------------|--------------------|
| `read_file`  | read a file (line-numbered) | no |
| `list_dir`   | list a directory            | no |
| `write_file` | create/overwrite a file     | **yes** |
| `edit_file`  | exact-string replace        | **yes** |
| `run_bash`   | run a shell command         | **yes** |

Mutating tools ask before running: `y` (yes) · `n` (no) · `a` (always allow this tool
this session) · `q` (abort the turn). Toggle with `/tools on|off`; skip all prompts with
`/auto on` (one-shot / piped mode auto-approves).

Tool calling is implemented for Anthropic, OpenAI, and Gemini. Generic
`openai-compatible` endpoints get tools too if they support OpenAI function calling.

## All commands

```
/help                              this help
/provider add|list|remove|set      manage providers
/use <provider> [model]            switch provider
/model [name] · /model list        show/set model
/models [provider]                 list models from the API
/system [prompt|clear]             set a custom system prompt
/tools [on|off]                    toggle file/shell tools
/auto [on|off]                     auto-approve tool calls
/set max_tokens|temperature <v>    tune generation
/clear                             reset the conversation
/history                           list messages so far
/save [path] · /load [path]        persist / restore a session
/config                            show config file location
/exit · /quit                      leave
```

During a response, **Ctrl-C** interrupts. **Ctrl-D** or `/exit` quits.

## Where config lives

`~/.config/nexais/config.json` (override with `NEXAIS_CONFIG_DIR` or `XDG_CONFIG_HOME`),
written `0600` since it holds API keys. Keys can also come from environment variables —
a provider with no stored key falls back to the matching env var at request time.

## Design

- `nexais/providers/` — one adapter per API, all converting to/from a single normalized
  `Message`/`Event` model. Adding a provider = one file + a registry entry.
- `nexais/tools.py` — built-in tools with JSON-Schema specs advertised to any provider.
- `nexais/config.py` — persistent providers, model selection, settings.
- `nexais/cli.py` — the REPL, the agentic tool loop, and slash-command dispatch.

## License

MIT
