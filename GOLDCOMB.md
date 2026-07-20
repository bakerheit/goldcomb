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
