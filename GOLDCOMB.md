# GOLDCOMB.md — project memory

## What this is
`goldcomb` — a provider-agnostic, Claude-Code-style terminal AI agent (Python 3.10+).
Installed command is `goldcomb` (see install.sh); package/entry is `goldcomb` (`python -m goldcomb`).

## Build / test / run
- Tests: `.venv/bin/python -m pytest` (pytest; suite in `tests/`, ~400 lines, thin coverage of cli.py).
- Lint: flake8 (config in `.flake8`).
- Run locally: `.venv/bin/goldcomb` or `.venv/bin/python -m goldcomb`.
- Deps: httpx, rich, prompt_toolkit.

## Layout
- `goldcomb/cli.py` (~1300 lines) — REPL, agentic tool loop, slash commands (`cmd_*` dispatch, `COMMANDS` list).
- `goldcomb/providers/` — one adapter per API (anthropic, openai, gemini, base); normalized Message/Event model. New provider = one file + registry entry.
- `goldcomb/tools.py` — built-in tools (read_file, write_file, edit_file, list_dir, run_bash) + guardrails (catastrophic-command regexes, disk-free sentinel MIN_FREE_MB).
- `goldcomb/git_tools.py` — read-only, structured git tools (git_status/git_diff/git_log/git_branch; argv-only, 15s timeout, clean `{error}` dicts for git-missing/not-a-repo/empty-repo, 30k-char truncation). Agent-facing wrappers in `tools.py`; the `--serve` NDJSON `git_status` command (see `server.py`) emits a `git_status` event `{branch,ahead,behind,files}` (or an `error` event).
- `goldcomb/roles.py` — `--role` personas: `planner` (Tickets-board steward) and `advisor` (per-project financial advisor: cost/budget tracking, `.ai/finance/ledger.md` ledger, accounting setup help).
- `goldcomb/config.py` — persistent config at `~/.config/goldcomb/config.json` (0600), env-var key fallback.
- `goldcomb/threads.py` — autosaved session threads per project dir; `-c` / `-r` resume. Canonical store is `<config_dir>/projects/<cwd-key>/threads/` (full-fidelity JSON). Every save is also exported (best-effort) in a vendor-neutral interchange format at `<cwd>/.ai/threads/<id>.jsonl` (header line + `{"role","content"}` per line, `agent` field in header; a README.md there documents the format). Threads other tools write into `.ai/threads/` (agent != "goldcomb") are adopted into the canonical store on list/load — import is once-only (existing canonical id = skip). Deletes never prune the export.
- `goldcomb/ui.py` — spinner, markdown render, status bar.
- `goldcomb/presets.py` — provider presets for `/setup` and `/provider add <name>`.
- `goldcomb/pricing.py` — best-effort per-model prices for `/models`. Probes `GET <base_url>/models` on openai-compatible endpoints for OpenRouter-shaped `pricing.{prompt,completion}` (USD/token); shown as `$prompt/$completion per 1M tok` (`free` when both 0). No other provider API exposes prices, so nothing else displays them.

## macOS app (`macos/Goldcomb/`, SwiftPM, `swift build` / `swift run`)
- SwiftUI frontend over `goldcomb --serve` (NDJSON stdio, see `goldcomb/server.py`). Protocol supports `threads`/`resume` commands + `cwd` in the `ready` event; history also on disk at `<project>/.ai/threads/*.jsonl` (vendor-neutral).
- Sidebar = collapsible project sections (`Project`: name + folder) with agents nested underneath; ungrouped agents in an "Ungrouped" section. Selection is `SidebarItem` enum (`.project`/`.agent`) on `SessionStore`; `selectedSession` resolves the detail pane (project → its most recent agent).
- Sheets: `NewProjectSheet` (name + folder picker), `NewSessionSheet(project:)` (project set ⇒ folder fixed to the project's). Project actions (New agent / Rename… / Remove project…) are a shared `projectActions` builder in `ProjectHeader`, presented both as a right-click context menu and a visible `ellipsis` `Menu` button (borderless, indicator hidden) in the header row; actions reach `ContentView` via `NotificationCenter` (`.newAgentRequested` / `.renameProjectRequested` / `.removeProjectRequested`) — sheet/alert state lives in `ContentView`. Remove-project is confirm-gated (alert) and never deletes files: `SessionStore.removeProject` only stops the project's agents and drops it from `store.projects`.
- Agents tab: per-project agent tree (`AgentsTabView`; `AgentSession.parentID`, `SessionStore.children/treeRoots/teamContext/removeFromTree`). Add root/report via sheet with a CLI persona (`personaRole`: worker/planner/advisor) plus independent user-facing `role` and `description` metadata; remove reparents children upward and rows jump to chat. `teamContext(for:)` renders "Your lead/teammates/reports" and is passed as `--team` at launch (snapshot: applies on next start). Persisted via `SavedAgent.parentID`.
- Sidebar state persists (NEXA-8): projects + agents (name/folder/sudo/personaRole/display role/description) are saved to `~/Library/Application Support/Goldcomb/SidebarState.json` (versioned Codable, pretty JSON, written on every mutation) and restored at startup — restored agents relaunch their `goldcomb --serve` process. `AgentSession.personaRole` alone maps to CLI `--role`; `AgentSession.role` is display metadata and renders as a sidebar badge immediately left of `sudo`. Legacy state lacking `personaRole` migrates its old `role` value to the persona and leaves the display role empty. Only organizational state lives there; transcripts stay in each project's `.ai/threads/`.
- Right-side file explorer (`FileExplorer.swift`): `FileNode`/`FileExplorerModel` lazy tree (dirs load on expand, skip-list hides `.git`/`node_modules`/`.venv`/etc.), `FileExplorerView` in the detail pane next to `ChatView`, toggled by a `sidebar.right` toolbar button (`SessionStore.explorerVisible`, persisted). One model per agent in `store.explorers`; auto-refreshes (expansion preserved) when `session.isRunning` goes false so agent-made edits appear. `List(children:)` needs an explicit data collection — the bare single-root form doesn't compile.
- Verify with `cd macos/Goldcomb && swift build` (Xcode also works; macOS 14+ target).

## Conventions / gotchas
- Never interpolate exception text (or any model/user-derived string) into `console.print` unescaped — Rich parses `[...]` as markup and a stray `[/dim]` in an error message once crashed the REPL's own error handler. Always `from rich.markup import escape` and wrap with `escape(str(e))`.
- Mutating tools confirm before running; `/auto on` and one-shot/piped mode auto-approve.
- Tool output capped at MAX_OUTPUT = 30k chars.
- No context-compaction or API retry/backoff exists yet (known gaps).
- README documents every user-facing feature — update it when adding commands/tools.

## Agent identity model (decided NEXA-30, 2026-07-19)
- Canonical identity = the agent's NAME: one string, set by the launcher
  (macOS app: `AgentSession.name`; CLI: `--agent-name`; default `goldcomb`).
  That one value feeds all four identity sinks and they must never split:
  thread headers (`threads.AGENT_NAME`), scrum assignees (`scrum.CURRENT_AGENT`),
  memory file slug (`.ai/memory/<slug>.md`), and recall's "mine" filter.
- Persona (`--role planner|advisor`) is BEHAVIOR, not identity: a system-prompt
  block only. Never stamped, never matched. The app's display `role` badge is
  likewise pure metadata.
- Sub-agents: thread headers carry `goldcomb-subagent:<label>` (provenance);
  the durable person is the bare LABEL (memory + assignees). A lead agent does
  not own its workers' threads in personal history — it sees them via
  `recall all=true` / the Project tab.
- Legacy aliases (read-time only, never rewrite files): `nexais` ≡ `goldcomb`,
  `nexais-subagent:<label>` ≡ `goldcomb-subagent:<label>`, and format
  `nexais.ai-thread` stays readable (`threads._LEGACY_FORMATS`). GUI/CLI
  matching use a shared `matches(name, headerAgent)` helper with these aliases
  instead of bare `==`: Swift `AgentIdentity` (NEXA-29) and Python
  `goldcomb/identity.py` (NEXA-31) — kept in lockstep, since one side writes
  the headers the other reads. Recall's "mine" filter is
  `matches(name, header) and not is_subagent(header)`; memory is unchanged
  (files are written under the live name, no cross-era discovery needed).
- Naming discipline: an app agent keeps one name for life (persisted in
  SidebarState.json); workers are redeployed under the same label. Renames
  fork history (old threads stay under the old name) — acceptable, documented.

## Agent memory & recall (2026-07-19)
- `goldcomb/memory.py`: per-agent `.ai/memory/<slug>.md`, MAX_CHARS cap, tool
  actions show/remember/rewrite; identity = scrum.CURRENT_AGENT; sub-agents
  inherit via `subagent_system_prompt(label)`.
- `goldcomb/recall.py`: reads `.ai/threads/*.jsonl` headers (the only store
  with agent identity); tool actions list/read/search (+all=true for other
  agents); `digest()` puts the agent's recent threads in its system prompt
  (current thread excluded).
- `/memory` prints the agent's file. Both tools always available (not
  scrum-gated); registered in tools.py next to `_scrum_run`.

## Group-chat @-tagging & composer (2026-07-20)
- `@name` tags an agent as the *expected* responder without silencing the room.
  Semantics live in `ChatRoom.addresses`/`expects`/`taggedAgents`: an agent
  message stays targeted (naming someone wakes only them; an untagged agent
  remark wakes nobody — anti-ping-pong), but a **user** post always broadcasts
  (the user's floor), so a tagged user post wakes the tagged agent AND the
  others — the tag sets expectation, not exclusivity. The broker digest
  (`SessionStore.chatDigest`) frames it three ways: tagged → "a reply is
  expected"; untagged-but-someone-else-tagged → "chime in only if you have
  something specific"; open broadcast → "respond if you have something to add".
  Cost note: a tagged user post wakes all agents (bounded by the unattended cap
  + the decline-if-nothing digest), which is the tradeoff for "others may chime
  in".
- Composer parity: `ChatRoomView`'s composer gains inline `@`-mention
  autocomplete — typing `@` opens a popup that filters agents as you type
  (`MentionAutocomplete`: pure trigger/filter/apply logic, prefix-first
  substring search; Return picks the top, click picks any), plus a discovery
  button and a live "expecting a reply from …" hint, alongside the existing
  attach. What autocomplete inserts (`@Given `) is exactly what the broker
  matches. Session-only controls from the single-agent chat (model picker,
  /compact, /clear, sudo, new-conversation) don't apply to a multi-agent room
  and are intentionally omitted.

## Deployed agents → roster & config (2026-07-20)
- **Persist (Swift).** `promoteDeploys` dropped its 20-minute age window: every
  deployed worker (`deploy_agent`) becomes a permanent, configurable roster
  member parented under its deployer — a deploy run hours ago still joins the
  Agents tab. Once promoted it persists; the user removes it explicitly, and
  `declinedPromotions` is now persisted to UserDefaults so a removed agent
  doesn't resurrect from its lingering on-disk record on the next launch.
- **Govern (Python bridge).** The app publishes each project's per-agent default
  models to `<project>/.ai/agents/agent-config.json`
  (`SessionStore.writeAgentConfigs`, keyed by agent name, written alongside the
  sidebar state). The deploy flow consults it: `agents.configured_default(name)`
  reads that file, and `cli._run_subagent` uses the configured model when the
  deploying agent didn't pin one — so a lead deploying a pre-configured agent
  runs it on the model the user chose. Matches the full human name, falling back
  to the bare `(label)`.
- The loop: deploy once → it joins the roster → configure its default model in
  the config sheet → subsequent deploys of that label honor it. An explicit
  `provider`/`model` on the deploy call still wins over the config.

## Agent config sheet (2026-07-20)
- Clicking an agent card in the Agents tab opens `AgentConfigSheet` (its
  settings), not a chat — chat now lives on the card's message icon and the
  context menu's "Open chat". The sheet shows read-only identity (name, persona,
  live model, folder) and edits: display **role** + **description** (app-side
  metadata, now `@Published var` on AgentSession, live via
  `SessionStore.updateAgentMetadata` — never passed to the process, so no
  restart), **default model** (the picker moved here from the card context
  menu), and **sudo** (live toggle). "Open chat" in the sheet routes to the
  agent's chat.

## Per-agent default model (2026-07-20)
- An agent can have a user-chosen default model, set from the Agents tab (card
  context menu → "Default model") or promoted from the chat model chip ("Set …
  as default"). Stored on `AgentSession.defaultProvider/defaultModel`, persisted
  via `SavedAgent.provider/model` (repurposed from the old "last used" — a live
  chip change no longer persists as the default).
- `AgentSession.serveArguments` passes it as `--provider`/`--model` at launch
  (the CLI's `--serve` already honors those, in-memory), so the agent runs on
  its own model whenever its process starts — including when woken for a group
  chat or delegated to — not just when the user opens its chat. No default →
  inherit the app's global model. `SessionStore.setAgentDefaultModel` persists
  it and applies it live (`use`) if the agent is running.
- Live chip changes stay per-session (in-memory `use`), distinct from the
  default. Scope note: the in-process `deploy_agent` worker (a lead choosing a
  model for an ephemeral labelled worker) is NOT covered — this is about
  app-launched agent sessions, which is what group chat and chat-based
  delegation use.

## Chat scroll-to-bottom on open (2026-07-20)
- ChatView's transcript only scrolled to the newest message `.onChange` of the
  transcript count/last text — which don't fire for a transcript that's already
  populated when the view appears (switching agents, or after a resume hydrates
  history), so it opened at the first message. Added `.onAppear` →
  `scrollToEnd(animated:false)` (deferred one runloop tick so the LazyVStack has
  laid out its rows). The `.onChange` handlers now share the same helper.
  ChatRoomView already scrolled on appear (jump-to-first-unread).

## History popover load fix (2026-07-20)
- The ChatView history popover loaded its thread list in the History button's
  action — the same render cycle that flipped `showHistory`, so the popover's
  first presentation raced the `@State` update and came up empty; it only
  filled on a second open. Moved the load into a `reloadHistory()` helper called
  from the popover's `.onAppear` (kept in the button action too, so the common
  case has no flash). Fires each open, so newly saved conversations show up. The
  ProjectView thread list already used the correct `.onAppear`/`.onReceive`
  pattern.

## Markdown rendering in chat (2026-07-20)
- Agent messages are full of Markdown (headings, lists, bold, quotes, fenced
  code). SwiftUI's `AttributedString(markdown:)` only does *inline* syntax, so
  block elements came through raw. `MarkdownText.swift` adds a line-oriented
  block parser (`Markdown.parse → [MarkdownBlock]`, pure/tested) and a
  `MarkdownMessage` view that renders headings, ordered lists (source numbers
  preserved), bullet lists, blockquotes, fenced code, and rules — inline syntax
  within each block still goes through `AttributedString(markdown:)`.
- Used by both the single-agent transcript (`ChatView` assistant rows) and the
  group-chat bubbles (`ChatRoomView`). Ticket-id links and the @user highlight
  (NEXA-84/70) survive via `ChatLinkRouter.decorate`, an attribute-only overlay
  applied on top of the Markdown-parsed runs (range-mapped by character
  distance, so it composes without disturbing bold/italic/code). The old
  word-by-word `ChatLinkRouter.attributed` now delegates to `decorate`.

## Model picker & live catalog (2026-07-20)
- The serve `ready` event's per-provider `models` uses `_models_for` =
  `cached or default_models_for(type)` (server.py), so a freshly-added provider
  surfaces its built-in model list instead of an empty one that renders as just
  "default model" in the app's picker. Mirrors the CLI's fallback.
- A `models` serve command live-fetches a provider's full catalog
  (`provider.list_models()`), caches it, and replies with a `models` event
  `{provider,models,ok,error?}` — a fetch failure still returns the built-in
  list. The app's model chip has a "Refresh from API" action per provider
  (`AgentSession.refreshModels`) that drives it; the reply updates
  `knownProviders`. Without this the app only ever had the built-in list from
  the ready event.

## Slash commands & /compact (2026-07-20)
- The macOS composer (`ChatView`) has slash commands (`SlashCommands.swift`):
  type `/name` (palette suggests as you type) or use the `slash.circle` menu.
  Each maps to an existing `AgentSession` command so app and CLI stay in step;
  zero-argument by design. Current set: `/compact`, `/clear`, `/sudo`.
  `SlashCommands.match` runs a bare `/name`; a line with a space is a message,
  never a command.
- `/compact` summarizes the conversation and continues from the summary — the
  same context-cost lever as the group-chat work, but user-driven. CLI
  `App.compact_conversation` makes one no-tools provider call
  (`COMPACT_SYSTEM`), then replaces history with a single user message
  (`COMPACT_PREFIX` + summary); the thread is kept (unlike `/clear`), so the
  next autosave writes the compacted history. Guards: needs ≥4 messages, and an
  empty summary leaves history untouched. Serve wiring: a `compact` command in
  `server.py` → a `compacted` event `{ok,before,after,reason}`; the app resets
  `isRunning` and logs the outcome (the compact command is not a turn, so
  nothing else clears the spinner). The summarization call itself costs tokens
  (full transcript in), so it pays off only once a conversation is long enough
  that future turns would re-send that history many times.

## Agent chats & attachments (NEXA-64, 2026-07-20)
- Rooms are append-only JSONL at `.ai/chats/<id>.jsonl` (`goldcomb/chats.py`
  owns the format; Swift `ChatRooms.swift` mirrors the reader). Delivery is the
  macOS broker's job (`SessionStore.deliverChats`): only agents a message
  *addresses* are woken (`ChatRoom.addresses`), and an unaddressed post is left
  for the human — this is what keeps a group chat from ping-ponging on the
  API bill.
- Sidebar chats (NEXA-71): `SidebarItem.chat(roomID)` renders a per-project
  CHATS section; the detail pane and the Chats tab both use the one shared
  `ChatRoomView`. Unread-badge re-diff rides the NEXA-43/44 path — chat state is
  folded into the sidebar List's pulse and `store.chatReadTick` bumps on read.
  Agent-only DM rooms (no `user` participant) show but are read-only (NEXA-69).
- Attachments (NEXA-74/75/78, Phase A) are **references, never inline base64**:
  a message line gains optional `attachments:[{name,path,mime,size}]`; bytes are
  copied into a per-room sidecar `.ai/chats/attachments/<chat-id>/<ts>-<name>`
  with a **project-relative** `path` (no absolute user path leaks into a
  digest). Invariants: copy **before** appending the line (no dangling
  reference); **both writers** — `chats.py` (agents, via the `chat` tool's
  `attachments:[paths]`) and the app composer (`ChatRoom.storeAttachment`) —
  use the identical layout; a missing key is tolerated so old readers need no
  lockstep upgrade (no format-version bump). Agents consume with `read_file`;
  images carry an explicit "you cannot view images yet" line (true vision is
  Phase B, held) so a text-only model doesn't hallucinate seeing them — this
  wording is shared word-for-word between `chats.py _attach_line` and the Swift
  broker digest / `ChatAttachment.digestLine`.
- Ticket links (NEXA-84): `NEXA-<n>` in a transcript renders as a link
  (`ChatLinkRouter`, `goldcomb://ticket/<id>`); tapping routes through
  `SessionStore.focusTicket` → selects the room's project and opens its Sprint
  tab (`pendingTicketFocus`, consumed by `ProjectDetailView`).

## Claude mode — pluggable engine (2026-07-21)
- Two execution engines, chosen by `App.engine` ("native" default | "claude"):
  `/mode [native|claude]` (alias `/engine`), the `--engine` launch flag, or the
  persisted `settings["engine"]`. `--engine` is in-memory only (set on
  `cfg.settings` before `App`/`serve`); `/mode` persists via `set_setting`.
- **native** = the existing `cli.py` tool loop over `Provider.stream` + `tools.py`
  (any provider). **claude** = delegate the turn to the **Claude Agent SDK**
  (`claude-agent-sdk`, optional dep, `pip install "goldcomb[claude]"`), which
  bundles the Claude Code CLI and runs its own loop/tools. Anthropic-only.
- Integration trick: `engines/claude.py::ClaudeEngine` implements the
  `Provider.stream` contract but emits **one** assistant turn with **no**
  `tool_calls` (the SDK executes tools itself), so `_drive_turn`/`_stream_once`
  run it unchanged. `get_provider()` returns a `ClaudeEngine` when `engine=="claude"`
  (dropping a non-Anthropic provider's api_key so it can't leak in as
  `ANTHROPIC_API_KEY`). Tool activity is surfaced as inline `TextDelta`s that are
  shown but NOT folded into the saved message.
- Async→sync bridge: `run_async_stream` runs the SDK's async `query()` on a worker
  thread + `queue.Queue`, yielding to the sync generator; SDK errors (CLINotFound…)
  wrap as `ProviderError`. SDK types matched by class *name* (`AssistantMessage`,
  `TextBlock`, …) so `tests/test_claude_engine.py` is hermetic (fake `query_fn`, no
  SDK/network; `sys.modules["claude_agent_sdk"]=None` forces the not-installed path).
- Auth: the SDK reads `ANTHROPIC_API_KEY` (passed via subprocess env, merged over
  `os.environ`) or Claude Code's own on-disk login for a subscription. It does NOT
  take goldcomb's `/login` OAuth token programmatically — claude-mode subscription
  auth is a *separate* Claude Code login. permission_mode = "acceptEdits" (or
  "bypassPermissions" under `--sudo`); override with `settings["claude_permission_mode"]`.
- **Known gaps (first cut, follow-ups):** no interactive per-tool confirmation
  (would need a `can_use_tool` bridge to goldcomb's `_confirm`); goldcomb's
  memory/recall/scrum/sub-agents don't work inside a claude turn (re-expose as SDK
  custom/MCP tools); cross-turn memory is best-effort via prompt context (a
  persistent `ClaudeSDKClient` would fix it); block-level not token-level streaming
  (`include_partial_messages`); no macOS `--serve` NDJSON adapter / app toggle yet;
  **live path unverified** (needs the SDK installed + auth), like the OAuth work.
- Built on committed HEAD (bb60170), so this branch does NOT include the in-flight
  uncommitted OAuth work — additive + defensive, composes when that lands.
