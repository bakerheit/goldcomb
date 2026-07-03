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

**One command (recommended)** — creates a virtualenv and a short `nexai` command on your PATH:

```bash
./install.sh              # command: nexai   (into ~/.local/bin)
./install.sh myai         # name it something else
./install.sh nexai --bin /usr/local/bin
```

If the target bin dir isn't on your PATH, the script prints the exact `export PATH=…` line to add.

**Manual** — if you'd rather manage it yourself:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nexais           # or:  python -m nexais
```

Dependencies: `httpx`, `rich`, `prompt_toolkit`.

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

**Easiest: `/setup`** — a guided menu. Pick a provider from a numbered list, paste your
key, and you're done. base URLs and default models are filled in for you. On first run
(with no provider configured) this launches automatically.

```
› /setup
─────────────────── Add a provider ───────────────────
Which provider would you like to use?
   1  Anthropic — Claude
   2  OpenAI — GPT
   3  Google — Gemini
   4  OpenRouter — hundreds of models via one key
   5  Groq — very fast open models
   6  DeepSeek
   7  Mistral
   8  Together AI
   9  Ollama — local models (no key)
  10  LM Studio — local models (no key)
  11  Custom — any OpenAI-compatible endpoint (you give a URL)
Enter a number or name (blank to cancel): 4
Need a key? Get one at: https://openrouter.ai/keys
Paste your OpenRouter API key: ****
✓ Added 'openrouter' (openai-compatible)
```

**Shortcut: preset by name.** Any preset can be added in one line — base URL and default
model come from the preset, and the key is prompted for (hidden):

```
/provider add anthropic       # or: openai, gemini, openrouter, groq,
/provider add groq            #     deepseek, mistral, together, ollama, lmstudio
```

**Full control (scriptable):**

```
/provider add <name> <type> [api_key] [base_url]
```

`type` is one of `anthropic`, `openai`, `gemini`, `openai-compatible` (aliases:
`claude`, `gpt`, `google`, `openrouter`, `groq`, `ollama`, …). If you omit the key it's
prompted for (hidden). `openai-compatible` requires a `base_url`.

```
/provider add oai      openai        sk-...
/provider add local    openai-compatible  ""  http://localhost:11434/v1   # Ollama
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
/model <number>             pick by number from the last /models list
/model list                 known models for this provider type
/models [provider] [filter] numbered live model list (filter is a substring)
```

`/models` prints a numbered list; then `/model 7` selects the 7th. Right after adding a
provider, nexais offers to fetch its model list so you can pick without typing an ID.

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

In agentic mode the model is instructed to orient itself first, **verify its work** by
running it (trying `python3 -m …` / `pip3` / a local `.venv` before giving up), re-read a
file after a failed edit instead of guessing twice, and finish with a one-line status.
`edit_file` echoes the changed region back so the model can see the result. Each response
prints its token usage (`↳ N in / N out`), and if a run hits the tool-call ceiling it
forces a final summary of what's done and what's still broken.

## Memory across runs

Each `nexai -p` is a fresh conversation by default. Two mechanisms carry context forward:

- **`NEXAIS.md`** — if a `NEXAIS.md` (or `.nexais/memory.md`) exists in the working
  directory, its contents are loaded into the system prompt, and the agent is told to
  record durable facts there (the exact build/test/run commands, layout, gotchas) as it
  learns them. Drop notes in that file and every future run starts informed — no re-reading
  the whole project to rediscover how to test it.
- **`-c` / `--continue`** — continue the previous one-shot session in this directory
  (persisted to `./.nexais/session.json`):

  ```bash
  nexai -c -p "build a CLI that counts words in a file"
  nexai -c -p "now add a --json flag"     # remembers the previous turn
  ```

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
