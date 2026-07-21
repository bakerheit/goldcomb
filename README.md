# goldcomb

A **Claude-Code-style terminal AI agent that works with any provider.** Configure
providers and switch models with slash commands — no code changes, no restarts.

Talk to Anthropic Claude, OpenAI, Google Gemini, or any OpenAI-compatible endpoint
(OpenRouter, Groq, Together, Ollama, LM Studio, vLLM, …) from one CLI, with streaming
responses and built-in file/shell tools so the model can actually do work.

```
┌ goldcomb — multi-provider AI agent for the terminal ┐
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

Requires Python 3.10+ (check with `python3.10 --version`).

**One command (recommended)** — creates a virtualenv and a short `goldcomb` command on your PATH:

```bash
./install.sh              # command: goldcomb   (into ~/.local/bin)
./install.sh myai         # name it something else
./install.sh goldcomb --bin /usr/local/bin
```

If the target bin dir isn't on your PATH, the script prints the exact `export PATH=…` line to add.

**Manual** — if you'd rather manage it yourself:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/goldcomb           # or:  python -m goldcomb
```

Dependencies: `httpx`, `rich`, `prompt_toolkit`.

## Quick start

```bash
goldcomb                    # interactive session
goldcomb -p "what is 2+2"   # one-shot answer
echo "explain this" | goldcomb   # piped input
```

On first run, goldcomb auto-detects any of these environment variables and configures a
provider for you: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MOONSHOT_API_KEY`. Otherwise,
add one with a slash command.

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
   7  Moonshot AI — Kimi
   8  Mistral
   9  Together AI
  10  Ollama — local models (no key)
  11  LM Studio — local models (no key)
  12  Custom — any OpenAI-compatible endpoint (you give a URL)
Enter a number or name (blank to cancel): 4
Need a key? Get one at: https://openrouter.ai/keys
Paste your OpenRouter API key: ****
✓ Added 'openrouter' (openai-compatible)
```

**Shortcut: preset by name.** Any preset can be added in one line — base URL and default
model come from the preset, and the key is prompted for (hidden):

```
/provider add anthropic       # or: openai, gemini, openrouter, groq, deepseek,
/provider add kimi            #     mistral, together, ollama, lmstudio
```

**Full control (scriptable):**

```
/provider add <name> <type> [api_key] [base_url]
```

`type` is one of `anthropic`, `openai`, `gemini`, `openai-compatible` (aliases:
`claude`, `gpt`, `google`, `openrouter`, `groq`, `kimi`, `ollama`, …). If you omit the key it's
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

### Scriptable provider configuration

The one-shot management interface is intended for native clients and automation. Reads are
JSON-only and never include API-key material:

```bash
goldcomb config presets --json   # known-provider presets (type, base URL,
                                 # default model, env var) for an "Add provider"
                                 # picker — so a user only needs to paste a key
goldcomb config list --json
goldcomb config add --json --name work --type openai \
  --default-model gpt-4o --api-key-stdin < key.txt
goldcomb config update --json --name work --new-name office \
  --type openai --default-model gpt-4.1 --current
echo "$OPENAI_API_KEY" | goldcomb config update --json --name office --api-key-stdin
```

Keys are accepted only from standard input with `--api-key-stdin`, never as command-line
arguments (which can be visible to other processes). List and mutation responses omit
`api_key` entirely and report only `has_key` plus `key_source` (`config`, `env`, or `none`).
Provider names must match `[a-z0-9-]+`; types are `anthropic`, `openai`, `gemini`, or
`openai-compatible`. Remote base URLs must use HTTPS; HTTP is accepted only for local
endpoints. `openai-compatible` providers require a base URL. The config is locked during
updates, replaced atomically, and kept mode `0600` because it may contain secrets.

## Switch models with a slash command

```
/use <provider> [model]     switch active provider (and optionally model)
/model <name>               set the model on the current provider
/model <number>             pick by number from the last /models list
/model list                 known models for this provider type
/models [provider] [filter] numbered live model list (filter is a substring)
/models all                 include non-chat models (embeddings, tts, image, …)
                            prices shown when the provider's API publishes them
                            (e.g. OpenRouter): $prompt/$completion per 1M tokens
```

`/models` fetches the live catalog straight from the provider's API (for OpenAI, the
`/v1/models` endpoint) and prints a numbered list; then `/model 7` selects the 7th. For
OpenAI it hides models you can't chat with — embeddings, TTS, Whisper, image, audio,
realtime, moderation — so the list is short and useful; add `all` to see everything.
Right after adding a provider, goldcomb offers to fetch its model list so you can pick
without typing an ID.

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
| `deploy_agent` | spawn an autonomous sub-agent | **yes** |
| `ask_user` | ask you clarifying questions | no |
| `scrum` | plan/track work on the project board (opt-in: `/scrum on`) | no |
| `memory` | the agent's private per-project memory file | no |
| `recall` | list/search/reread past conversations (own or any agent's) | no |

**Memory & recall.** Every agent keeps a private Markdown memory at
`.ai/memory/<agent>.md`, loaded into its system prompt each session and
maintained by the agent itself (`memory` tool: remember one durable fact,
rewrite to prune, show; `/memory` prints yours). Deployed sub-agents get the
same under their deploy label, so a recurring worker keeps its lessons. Agents
are also aware of their own history: their system prompt lists their recent
threads in the project, and the `recall` tool lists/searches/rereads past
conversations from `.ai/threads/` — their own by default, any agent's with
`all=true` (reading a teammate's handover). Both stores are vendor-neutral
files any AI tool can read or write.

**Planning with tickets.** Ticket tracking is **opt-in per project**: run `/scrum on`
(alias `/tickets on`) in a project — or hit "Enable ticket tracking" in the app's
**Tickets** tab — to create its board;
until then the scrum tool isn't even offered to the model, so agents never bureaucratize
repos that don't want a board. `/scrum off` hides it again without deleting anything,
and `/scrum` alone prints the board. Once enabled, the `scrum` tool gives the model a JIRA-style board — epics →
stories → tasks with points and enforced status transitions, plus sprints with burndown.
Every item is a **ticket** (`CF-7`) numbered in one sequence under the board's project
key (set with `key=` at init, or derived from the project name), and tickets carry
**assignees**: moving a task to `in_progress` auto-assigns it to the working agent's
identity, and `assign` hands tickets around explicitly. Identity comes from
`--agent-name` (the macOS app passes each agent's name; sub-agents claim tickets under
their deploy label), so the board always shows *which agents are working on which
tickets* — `show` prints an "In progress" section, and the app's Tickets tab has a
live "Working now" panel. Beyond the basics, tickets support **comments**
(`comment`, shown with `ticket_show`), **labels** (`labels='bug,ui'`, filter with
`task_list label=bug`), **dependencies** (`blocked_by='CF-3'` — a task refuses to
move to done while a blocker is open), free-text **search** (`find`), a persisted
**audit history** (`history` — every mutation, stamped with the acting agent), and
**deletion** (`task_del`/`story_del`/`epic_del`, with `force=true` guarding
non-empty deletes). For hands-off stewardship, launch an agent with
`--role planner` (or hit **Create planner agent** in the app's Tickets tab): a
scrum-master persona whose whole job is the board — grooming the backlog,
decomposing goals into tickets, running sprints, and giving standup reports —
and who files tickets for work instead of implementing it. The Tickets tab's
Standup / Groom backlog / Plan sprint buttons message the planner directly.
A second role is available independently of the ticket board: launch an agent
with `--role advisor` for an opt-in per-project **financial advisor** persona —
it tracks project costs (API/model spend, infra, tooling, subscriptions) in a
plain ledger at `.ai/finance/ledger.md`, sets and watches budgets, flags burn
rate, and helps with pricing considerations and accounting/bookkeeping setup as
the project turns into a business. It advises, records, and reports — it never
writes product code.
Every agent gets a **human name** ("Maya Trellis"), however it's created —
the app pre-fills one (roll the dice for another), and deployed sub-agents'
functional labels become "Ines Vale (retry-worker)" — so boards, trees, and
history read like a team, not a process list.
The app's **Agents** tab goes further: build a *tree* of agents per project
(lead → reports, e.g. a planner over several workers). The structure is
functional — each agent launches with a `--team` system-prompt block naming
its lead, teammates, and reports, so they hand work to each other by name
over the board. The tree persists and restores with the sidebar. The chat composer carries
the session controls: attach files (picker or drag-drop — paths go to the
agent, which reads them with its own tools), a live provider · model chip
that switches models in place, a sudo toggle, an interrupt button while a
turn runs, and one-click new-conversation (the old thread stays in history). The board persists at `<project>/.ai/scrum/board.json` in the
project's **`.ai` workspace folder** (alongside `.ai/threads/`), a vendor-neutral format
any AI tool can read or write — a `README.md` in the folder documents it. Boards from
the old `.goldcomb/board.json` location migrate automatically on first use.

**Sub-agents.** `deploy_agent` gives the model a way to hand a self-contained subtask
(a broad search, bulk edits, a long test run) to a fresh worker with its own context
and the same tools — minus `deploy_agent`, so workers can't recurse. The deploying
model may pick any configured provider/model for the worker (e.g. a faster model for
mechanical work) or omit them to inherit the session's current ones. Sub-agent tool
activity shows as nested `⏺` lines under the deploy call, its tokens count toward the
session totals, and only its final report returns to the lead agent. Approving the
deployment approves the worker's tool use — the `run_bash` guardrails still apply.

**Asking you questions.** `ask_user` lets any model pause mid-task and ask up to 4
clarifying questions, each with optional suggested answers — in the terminal you pick by
number (or type anything); in the macOS app you get a question sheet. The model is
instructed to use it only for decisions that are genuinely yours (preferences, scope,
hard-to-reverse choices), never for things its other tools can answer. Sub-agents can't
use it — they work autonomously. In non-interactive runs (`-p`, piped) the tool tells
the model to proceed on its best judgment.

Mutating tools ask before running: `y` (yes) · `n` (no) · `a` (always allow this tool
this session) · `q` (abort the turn). Toggle with `/tools on|off`; skip all prompts with
`/sudo on`, or start the session that way with `goldcomb --sudo` (one-shot / piped mode
auto-approves). The `run_bash` guardrails below still apply in sudo mode.

Tool calling is implemented for Anthropic, OpenAI, and Gemini. Generic
`openai-compatible` endpoints get tools too if they support OpenAI function calling.

In agentic mode the model is instructed to orient itself first, **verify its work** by
running it (trying `python3 -m …` / `pip3` / a local `.venv` before giving up), re-read a
file after a failed edit instead of guessing twice, and finish with a one-line status.
`edit_file` echoes the changed region back so the model can see the result. Each response
prints its token usage (`↳ N in / N out`), and if a run hits the tool-call ceiling it
forces a final summary of what's done and what's still broken.

**Guardrails.** `run_bash` refuses obviously catastrophic commands (`rm -rf /`, fork
bombs, `mkfs`, `dd` to a device) and runs everything under a **disk sentinel** — if free
space falls below a floor (default 500 MB, `GOLDCOMB_MIN_FREE_MB`), the command's whole
process tree is killed before it can fill the disk. Repeating the same failing command is
refused on the third try. Malformed tool calls return a correctable error instead of
crashing, and an unexpected error in a turn is reported with the conversation preserved —
never a hard crash.

## Memory across runs

Each `goldcomb -p` is a fresh conversation by default. Two mechanisms carry context forward:

- **`GOLDCOMB.md`** — if a `GOLDCOMB.md` (or `.goldcomb/memory.md`) exists in the working
  directory, its contents are loaded into the system prompt, and the agent is told to
  record durable facts there (the exact build/test/run commands, layout, gotchas) as it
  learns them. Drop notes in that file and every future run starts informed — no re-reading
  the whole project to rediscover how to test it.
- **Threads** — every interactive session autosaves to a *thread*, scoped to the project
  directory. The canonical copy lives under the config dir (not in your repo), and each
  save is also exported in a **vendor-neutral interchange format** at
  `<project>/.ai/threads/<thread-id>.jsonl` — one JSON object per line (a header line,
  then `{"role","content"}` messages) — so *any* AI tool can read a project's chat
  history or contribute its own: goldcomb adopts threads other tools write there, making
  them resumable with `-c`/`-r`. A `README.md` in that directory describes the format.
  (Add `.ai/threads/` to your project's `.gitignore` if you don't want history
  committed.) Resume one later instead of starting over:

  ```bash
  goldcomb -c                 # resume the most recent thread for this directory
  goldcomb -r <id>            # resume a specific thread by id or id-prefix
  ```

  and inside the REPL:

  ```
  /threads                 list this project's saved threads (newest first)
  /resume [number|id]      resume one (blank = pick from the list)
  /new                     start a fresh thread (the previous one stays saved)
  ```

  `-c` / `-r` also work in one-shot mode, so a scripted `goldcomb -p` can carry context
  forward across calls:

  ```bash
  goldcomb -c -p "build a CLI that counts words in a file"
  goldcomb -c -p "now add a --json flag"     # continues the same thread
  ```

## Interactive UI

The REPL renders like Claude Code: the transcript is **static** (finished messages and
tool output scroll naturally and survive in scrollback), while a **dynamic region** at
the bottom — the animated "Thinking…" spinner plus a pinned status bar — is redrawn in
place. Tool calls print above it as they happen, responses **stream as live markdown**
(code blocks, headers, lists, bold), and tool-output previews **shrink to fit** short
terminals. The status bar stays up while the model works *and* while you type:

```
 openai · gpt-4o   ⬆12.4k ⬇3.1k   ctx ~5.2k   tools sudo
```

provider · model · session tokens up/down · rough context size · active flags. Every
repaint re-reads the terminal size — and a `SIGWINCH` hook repaints instantly — so the
whole dynamic region reflows when you resize the window. Prefer plain streamed text?
`/render off` (markdown back on with `/render on`).

## macOS app (GUI)

`macos/Goldcomb/` is a SwiftUI app that runs **multiple agents in parallel** — each
agent is an isolated `goldcomb --serve` process with its own working folder,
conversation, provider/model, and sudo setting. Agents can also carry an optional
**display role** and description; the role appears as a sidebar badge immediately
before `sudo`. This organizational metadata is separate from the `worker` / `planner`
/ `advisor` CLI persona passed with `--role`. The sidebar groups agents under
**projects**: a project has a name and a folder you pick at creation, and every
agent created inside it runs in that folder. Its sidebar context menu renames it
or **removes it from the app** (with a confirmation — the folder on disk is never
touched). You get streamed responses, tool-call
bullets with collapsible output, sub-agent activity, tool-approval dialogs (or per-agent
sudo), an interrupt button (SIGINT, like Ctrl-C), and live token counts. A right-side
file explorer (toggle with the `sidebar.right` toolbar button) shows the active
project's folder as a lazy, expandable tree — it skips noise like `.git` and
`node_modules`, auto-refreshes when an agent finishes a turn, and opens files in the
default editor (or Finder/Terminal from the context menu).

Selecting a **project** in the sidebar opens its detail pane with three tabs —
**Project**, **Agents**, **Tickets**; selecting an **agent** opens its chat. The
Project tab is a live view of the
project's `.ai` workspace folder. Project shows the scrum board (sprint banner with a
points progress bar, status counts, a **"Working now" panel mapping agents to the
tickets they're on** — assignees that match an agent running in the app get a live dot —
and epics → stories → ticket rows with ids and `@assignee` chips) plus the saved
conversation history from `.ai/threads/`, refreshed every couple of seconds as agents
work — changes from *any* agent or tool appear, not just this app's. Hit **Resume** on a
past conversation to adopt it into the current agent: the transcript reloads on screen
and the model continues with full context.

```
cd macos/Goldcomb
swift run            # or: open Package.swift in Xcode and hit Run
```

The command used to start each agent defaults to this repo's virtualenv
(`~/workspace/goldcomb/.venv/bin/python -m goldcomb`) and can be changed in the app's
Settings. The **Providers** Settings tab lists, adds, and edits shared providers through
the one-shot Python interface; the Swift app never reads or rewrites `config.json`.
Existing keys are never displayed, and replacement keys are sent only over a private
stdin pipe, not in process arguments or logs. Provider changes apply to newly launched
agents. Any running agent whose startup config revision differs gets a **restart** badge
in the sidebar and Agents view; restart that agent to load the changed provider config.

## Headless serve mode

The GUI is powered by `goldcomb --serve`: a headless session speaking NDJSON over stdio —
every stdout line is one JSON event (`ready`, `status`, `delta`, `tool_call`,
`confirm_request`, `usage`, `turn_end`, `threads`, `resumed`, …), every stdin line one
command (`user`, `threads`, `resume`, `confirm`, `use`, `sudo`, `exit`). Human-facing
notices go to stderr, so stdout stays machine-clean. Sub-agent runs report
`subagent_start` (`id`, `label`, `task`, `parent`, `provider`, `model`) and
`subagent_end` (`id`, `label`, `stop_reason`, `iterations`, `tool_calls`, `usage`,
`transcript_path`) so a frontend can show live worker rows that die with the session. Any frontend can drive it; the full
protocol is documented in `goldcomb/server.py`. Provider/model switches over the protocol
are in-memory only, so parallel sessions never fight over the shared config file.

Chat history is reachable two ways: over the protocol (`{"type":"threads"}` lists saved
threads for the session's project, `{"type":"resume","id":...}` loads one into the
running session) and on disk — every save is exported into the project at
`.ai/threads/<thread-id>.jsonl` in a vendor-neutral format (header line + one
`{"role","content"}` message per line; documented by a `README.md` alongside), so any
frontend or AI tool can browse a project's history without a live session, and threads
written there by other tools show up in `threads`/`resume`. The `ready` event reports
the session's `cwd` so a frontend can locate that folder.

## All commands

```
/help                              this help
/provider add|list|remove|set      manage providers
/use <provider> [model]            switch provider
/model [name] · /model list        show/set model
/models [provider] [all]           list models from the API (add 'all' for non-chat)
/system [prompt|clear]             set a custom system prompt
/tools [on|off]                    toggle file/shell tools
/sudo [on|off]                     run tool calls without confirmation
/set max_tokens|temperature <v>    tune generation
/scrum [on|off|show]               per-project ticket tracking (opt-in)
/threads                           list saved threads for this project
/resume [number|id] · /new         resume a thread / start a fresh one
/clear                             reset the conversation
/history                           list messages so far
/save [path] · /load [path]        export / import a session file
/config                            show config file location
/exit · /quit                      leave
```

During a response, **Ctrl-C** interrupts. **Ctrl-D** or `/exit` quits.

## Where config lives

`~/.config/goldcomb/config.json` (override with `GOLDCOMB_CONFIG_DIR` or `XDG_CONFIG_HOME`),
written `0600` since it holds API keys. Keys can also come from environment variables —
a provider with no stored key falls back to the matching env var at request time.

## Design

- `goldcomb/providers/` — one adapter per API, all converting to/from a single normalized
  `Message`/`Event` model. Adding a provider = one file + a registry entry.
- `goldcomb/tools.py` — built-in tools with JSON-Schema specs advertised to any provider.
- `goldcomb/config.py` — persistent providers, model selection, settings.
- `goldcomb/cli.py` — the REPL, the agentic tool loop, and slash-command dispatch.

## License

MIT
