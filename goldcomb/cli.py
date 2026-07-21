"""goldcomb — a provider-agnostic, Claude-Code-style terminal AI agent.

Run ``goldcomb`` for an interactive session, or ``goldcomb -p "question"`` for a
one-shot answer. Configure providers and switch models with slash commands.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import uuid
from datetime import date
from getpass import getpass
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config
from .presets import PRESETS, PRESETS_BY_KEY, Preset
from .pricing import fetch_prices, format_price
from .providers import (
    Completed,
    Message,
    Provider,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    build_provider,
    default_models_for,
    normalize_type,
)
from .providers import PROVIDER_TYPES
from . import theme
from .theme import THEMES, gradient, make_console
from . import agents
from . import scrum as scrum_mod
from .tools import (
    TOOLS_BY_NAME,
    describe_call,
    missing_required_args,
    set_agent_runner,
    set_ask_runner,
    tool_specs,
)
from .ui import Renderer, clear_viewport
from . import threads

COMMANDS = [
    "help", "setup", "provider", "use", "model", "models", "system", "tools",
    "sudo", "render", "set", "theme", "scrum", "tickets", "memory", "clear",
    "compact", "history", "save", "load", "config", "exit", "threads",
    "resume", "new", "mode",
]

#: System prompt for the one-off summarization call behind /compact. The goal
#: is a hand-off note dense enough that the conversation can continue with the
#: original history dropped — so it keeps decisions and their reasons, current
#: state, and open threads, not a play-by-play.
COMPACT_SYSTEM = """\
You are compacting a long agent conversation so it can continue with far less \
context. Read the transcript and write a dense summary that a fresh instance \
could pick up from without the original. Preserve: what the user is trying to \
accomplish; decisions made and why; the current state (what's done, what \
works, what's broken); concrete specifics still in play (file paths, commands, \
values, identifiers); and open threads or agreed next steps. Drop pleasantries \
and superseded detail. Write it as notes, not prose — terse and factual. Do \
not ask questions or address the user; this is a note to the next instance."""

#: Prepended to the summary so the model reads it as prior context, not a new
#: request.
COMPACT_PREFIX = "[Earlier conversation, compacted to a summary:]\n\n"

MAX_TOOL_ITERATIONS = 30

# Substrings marking OpenAI-family models you can't hold a chat with — embeddings,
# image, audio, moderation, transcription, and legacy completion engines. These
# are hidden from /models by default (run "/models all" for the raw catalog).
_NON_CHAT_MARKERS = (
    "embedding", "tts", "whisper", "dall-e", "dalle", "gpt-image",
    "chatgpt-image", "-audio", "audio-", "realtime", "moderation",
    "transcribe", "babbage", "davinci", "computer-use", "-instruct",
    "sora",
)


def chat_models_only(models: list[str]) -> list[str]:
    """Drop obvious non-chat OpenAI models (see _NON_CHAT_MARKERS)."""
    return [m for m in models if not any(k in m.lower() for k in _NON_CHAT_MARKERS)]


AGENT_INSTRUCTIONS = """\
You are goldcomb, an AI coding agent running in the user's terminal. The working \
directory is {cwd}. Today's date is {date}. You have tools to read, write, and \
edit files and run shell commands. Prefer acting through tools over describing \
what you would do, and don't hand a task back to the user that you can do yourself.

Orient first. At the start of a task, check for a GOLDCOMB.md, README, or test \
config to learn how this project is built and tested, and match its existing \
conventions (test framework, layout, style) — don't introduce new ones.

Verify your work. After writing or changing code, run it or its tests. If a \
command seems missing (e.g. pytest, pip), try alternatives before giving up: \
`python3 -m pytest`, `python3 -m pip`, `pip3`, or a local virtualenv \
(`.venv/bin/python`). Don't report success you haven't checked.

Inspect git with the dedicated tools. For repo state use git_status, git_diff, \
git_log, and git_branch (they return clean structured summaries and handle \
edge cases) rather than run_bash("git ...").

Editing rules. Read a file before you edit it. After an edit, if the next \
command shows the SAME error, RE-READ the file to see its exact current \
contents before editing again — never guess twice. Never run the same failing \
command more than twice without changing your approach. For small files, prefer \
rewriting the whole file with write_file over a chain of edit_file calls \
(surgical edits are error-prone in indentation-sensitive languages).

Ask only when truly blocked. ask_user puts clarifying questions to the human. \
Use it sparingly — only for decisions you cannot resolve from the request, the \
code, or sensible defaults (preferences, scope, hard-to-reverse choices). \
Never ask what your other tools can tell you.

Delegate big subtasks. deploy_agent spawns an autonomous sub-agent with a \
fresh context and the same tools (it cannot deploy further agents). Use it \
for self-contained work — broad searches, bulk mechanical edits, long test \
runs — and give it a complete standalone brief: it cannot see this \
conversation or ask questions, and only its final report comes back. You may \
pick a different configured provider/model per worker when it fits the job \
(e.g. a faster model for mechanical work); by default it uses the current one.

Remember for next time. When you discover a durable project fact — the exact \
build/test/run command, the entry point, the layout, or a gotcha — record it in \
GOLDCOMB.md (create or append) so future sessions start informed. This is your \
memory across runs; you start each run fresh otherwise.

Finish cleanly. End with one short line: what you did and whether tests pass. \
Skip narrating a plan before each tool call. If you added or changed a test, \
don't stop while it is failing — fix it, or revert the broken change and say the \
work is unfinished. Never end a turn with a test you introduced left red."""


class App:
    def __init__(self, cfg: Config, console: Console | None = None):
        self.cfg = cfg
        self.console = console or make_console()
        self.messages: list[Message] = []
        # The active thread this conversation persists to. Created lazily on the
        # first saved turn so nothing is written for an empty/aborted session.
        self.thread: threads.Thread | None = None
        self._last_threads: list[threads.Thread] = []  # from the last /threads
        # Whether turns autosave to a thread. Off for bare one-shots so a quick
        # `goldcomb -p "..."` doesn't leave a thread file behind.
        self.persist = True
        self.approved_tools: set[str] = set()
        self.auto_approve = False
        # Execution engine: "native" (the tool loop below) or "claude" (delegate
        # to the Claude Agent SDK — see engines/claude.py and cmd_mode).
        self.engine = self.cfg.settings.get("engine") or "native"
        self._last_models: list[str] = []  # remembered from the last /models
        self._cmd_counts: dict[str, int] = {}
        self._edit_counts: dict[str, int] = {}
        self.session_tokens = {"in": 0, "out": 0}
        self.renderer = Renderer(
            self.console, markdown=bool(self.cfg.settings.get("render_markdown", True))
        )
        self.renderer.footer = self._footer_info
        # The deploy_agent and ask_user tools need config/providers/rendering,
        # which live here — inject the runners (last-constructed App wins,
        # which is the active one in the CLI).
        set_agent_runner(self._run_subagent)
        set_ask_runner(self._ask_user_impl)
        # The prompt_toolkit Style for the bottom toolbar, kept so /theme can
        # re-skin it mid-session (set by _build_prompt_session).
        self._pt_style = None

    # ---- provider / model helpers -----------------------------------------

    def get_provider(self) -> Provider:
        name = self.cfg.current_provider
        if not name:
            raise ProviderError(
                "No provider configured. Run  /setup  for a guided menu."
            )
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        if self.engine == "claude":
            from .engines.claude import ClaudeEngine
            from .providers import normalize_type
            claude_entry = dict(entry)
            # Claude mode always runs the Claude Code (Anthropic) harness. A
            # non-Anthropic provider's key must not leak in as ANTHROPIC_API_KEY,
            # so drop it and let the SDK use ambient auth / Claude Code's login.
            if normalize_type(entry.get("type", "")) != "anthropic":
                claude_entry.pop("api_key", None)
            return ClaudeEngine(name, claude_entry, auto_approve=self.auto_approve)
        return build_provider(name, entry)

    MEMORY_FILES = ("GOLDCOMB.md", "NEXAIS.md", ".nexais/memory.md")
    MEMORY_MAX_CHARS = 6000

    def system_prompt(self) -> str | None:
        parts = []
        if self.cfg.settings.get("tools_enabled"):
            try:
                cwd = Path.cwd()
            except OSError:
                cwd = Path("(working directory unavailable)")
            parts.append(
                AGENT_INSTRUCTIONS.format(cwd=cwd, date=date.today().isoformat())
            )
            mem_name, mem_text = self._read_memory_file()
            if mem_text:
                parts.append(
                    f"Project notes (from {mem_name} — keep this file updated):\n{mem_text}"
                )
            from . import memory as memory_mod
            from . import recall as recall_mod
            own_memory = memory_mod.read_memory()
            if own_memory:
                parts.append(
                    f"Your memory (private to you, from {memory_mod.memory_path()} — "
                    "maintain it with the memory tool: remember durable facts, "
                    "rewrite to prune):\n" + own_memory
                )
            history = self._recall_digest_once(recall_mod)
            if history:
                parts.append(
                    "Your recent conversations in this project (the recall tool "
                    "can search or reread them):\n" + history
                )
        from .roles import role_prompt
        role_block = role_prompt(self.cfg.settings.get("role"))
        if role_block:
            parts.append(role_block)
        team = self.cfg.settings.get("team")
        if team:
            parts.append(
                "Team context (this project's agent tree — coordinate through "
                "the ticket board: assign tickets to teammates by name, leave "
                "comments for handovers):\n" + str(team)
            )
        if self.cfg.settings.get("tools_enabled"):
            # Deliberately static. A live chat list used to hang off this
            # block, but it changed on every posted message — and because the
            # system prompt is the cached prefix for the whole conversation,
            # that invalidated the cache on exactly the turns that most needed
            # it. The chat tool's 'list' action covers the same ground on
            # demand, and new messages are delivered into the turn anyway.
            parts.append(
                "Teammate chat (the `chat` tool): DM the agent most familiar "
                "with a module, or start a group chat for discussions — the "
                "human user is in every group chat as 'user'. New messages "
                "addressed to you are delivered into your turns automatically; "
                "reply with chat post, or don't reply if you have nothing to "
                "add.\n"
                "Tag the teammate you expect a reply from with @name ('@Quill "
                "can you check X?'). A tagged agent is expected to answer; "
                "others may still chime in if they have something to add. An "
                "agent post that tags nobody is left for the human to read, so "
                "say who it is for when you need a reply."
            )
        user_sys = self.cfg.settings.get("system_prompt")
        if user_sys:
            parts.append(user_sys)
        return "\n\n".join(parts) if parts else None

    #: Sentinel for "digest not computed yet" — None is a real result.
    _RECALL_UNSET = object()

    def _recall_digest_once(self, recall_mod) -> str | None:
        """The recent-conversations block, computed once per session.

        It carries other threads' timestamps and message counts, so recomputing
        it every turn made the system prompt differ every turn — which is a
        full cache miss on the entire conversation. It is background context,
        not live state, so one snapshot per session is the honest reading.
        """
        cached = getattr(self, "_recall_digest", self._RECALL_UNSET)
        if cached is not self._RECALL_UNSET:
            return cached
        digest = recall_mod.digest(
            exclude_id=getattr(getattr(self, "thread", None), "id", None)
        )
        self._recall_digest = digest
        return digest

    def context_estimate(self) -> int:
        """Rough token estimate of the current conversation (~4 chars/token),
        for the status bar. Cheap — no file IO, no model call."""
        total = 0
        for m in self.messages:
            total += len(m.content or "")
            for t in m.tool_calls:
                total += len(t.name) + len(str(t.arguments))
        return total // 4

    def _footer_info(self) -> tuple[str, str]:
        """(left, right) text for the in-turn status footer. Mirrors the idle
        prompt toolbar so the bar never disappears during a turn."""
        left = (
            f"{self.cfg.current_provider or 'no-provider'} · "
            f"{self.cfg.current_model or 'no-model'}"
        )
        st = self.session_tokens
        flags = ["tools" if self.cfg.settings.get("tools_enabled") else "no-tools"]
        if self.auto_approve:
            flags.append("sudo")
        if not self.cfg.settings.get("render_markdown"):
            flags.append("plain")
        right = (
            f"⬆{_fmt_k(st['in'])} ⬇{_fmt_k(st['out'])} · "
            f"ctx ~{_fmt_k(self.context_estimate())} · {' '.join(flags)}"
        )
        return left, right

    def _read_memory_file(self) -> tuple[str | None, str | None]:
        try:
            cwd = Path.cwd()
        except OSError:
            return None, None
        for rel in self.MEMORY_FILES:
            p = cwd / rel
            try:
                if p.is_file():
                    text = p.read_text(errors="replace")[: self.MEMORY_MAX_CHARS]
                    return rel, text
            except OSError:
                continue
        return None, None

    # ---- the agentic turn --------------------------------------------------

    def run_turn(self, user_text: str) -> None:
        self.messages.append(Message(role="user", content=user_text))
        self._cmd_counts = {}
        self._edit_counts = {}
        # Re-hook SIGWINCH each turn: prompt_toolkit owns it while prompting.
        self.renderer.install_resize_handler()
        try:
            self._drive_turn()
        finally:
            # Persist after every turn (even a partial/interrupted one) so a
            # crash or Ctrl-C never loses the conversation.
            self._autosave()

    def _drive_turn(self) -> None:
        try:
            provider = self.get_provider()
        except ProviderError as e:
            self.console.print(f"[err]{escape(str(e))}[/err]")
            return

        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                message, stop = self._stream_once(provider)
                if message is None:
                    return
                if not message.content and not message.tool_calls:
                    # A blank model turn (cut stream, filtered output) must
                    # not enter history: providers reject empty assistant
                    # messages on every later request, poisoning the thread.
                    self.console.print(
                        "[warn]The model returned an empty response.[/warn]")
                    return
                self.messages.append(message)
                if (
                    stop == "tool_use"
                    and message.tool_calls
                    and self.cfg.settings.get("tools_enabled")
                ):
                    results = self._run_tools(message.tool_calls)
                    if results is None:  # user aborted the whole turn
                        return
                    self.messages.extend(results)
                    continue
                return

            # Hit the tool-call ceiling: force a final wrap-up instead of a bare
            # exit that leaves a half-finished, possibly broken state.
            self.console.print("[warn]Reached the tool-call limit — summarizing.[/warn]")
            self.messages.append(
                Message(
                    role="user",
                    content="You've reached the tool-call limit and cannot call more "
                    "tools. In 2-4 sentences, summarize what you changed, what "
                    "currently works, and what is still broken or unfinished.",
                )
            )
            message, _ = self._stream_once(provider, force_no_tools=True)
            if message is not None:
                self.messages.append(message)
        except KeyboardInterrupt:
            self.renderer.stop_all()
            self.console.print("\n[warn]⏹ interrupted[/warn]")
        except Exception as e:  # noqa: BLE001 - a bad turn must never crash the session
            self.renderer.stop_all()
            self.console.print(f"[err]Unexpected error:[/err] {escape(str(e))}")
            self.console.print(
                "[dim]The session is intact — your conversation is preserved.[/dim]"
            )

    # ---- thread persistence ------------------------------------------------

    def _autosave(self) -> None:
        """Persist the current conversation to its thread (created on demand)."""
        if not self.persist or not self.messages:
            return
        if self.thread is None:
            self.thread = threads.new_thread(
                provider=self.cfg.current_provider, model=self.cfg.current_model
            )
        self.thread.messages = [m.to_dict() for m in self.messages]
        self.thread.provider = self.cfg.current_provider
        self.thread.model = self.cfg.current_model
        try:
            threads.save_thread(self.thread)
        except OSError as e:
            self.console.print(f"[dim]Could not save thread: {escape(str(e))}[/dim]")

    def _adopt_thread(self, thread: threads.Thread, announce: bool = True) -> None:
        """Make ``thread`` the active conversation, restoring its messages."""
        self.thread = thread
        self.messages = [Message.from_dict(m) for m in thread.messages]
        if announce:
            self.console.print(
                f"[ok]Resumed thread[/ok] [cur]{thread.id}[/cur] "
                f"[dim]({thread.message_count} msgs · {thread.title})[/dim]",
                highlight=False,
            )

    def _stream_once(self, provider: Provider, force_no_tools: bool = False):
        use_tools = self.cfg.settings.get("tools_enabled") and not force_no_tools
        tools = tool_specs() if use_tools else None
        settings = self.cfg.settings
        text_acc: list[str] = []
        streaming = False  # have we begun rendering assistant text?
        completed: Completed | None = None
        self.renderer.start_status("Thinking")
        try:
            stream = provider.stream(
                self.messages,
                model=self.cfg.current_model or "",
                system=self.system_prompt(),
                tools=tools,
                max_tokens=int(settings.get("max_tokens") or 4096),
                temperature=settings.get("temperature"),
            )
            for ev in stream:
                if isinstance(ev, TextDelta):
                    if not streaming:
                        self.renderer.begin_message(
                            self.cfg.current_provider or "", self.cfg.current_model or ""
                        )
                        streaming = True
                    text_acc.append(ev.text)
                    self.renderer.message_delta(ev.text)
                elif isinstance(ev, ThinkingDelta):
                    self.renderer.update_status("Thinking")
                elif isinstance(ev, Completed):
                    completed = ev
            if streaming:
                self.renderer.end_message()
            self.renderer.stop_status()
        except ProviderError as e:
            self.renderer.stop_all()
            if streaming:
                self.renderer.end_message()
            self.console.print(f"[err]Error:[/err] {escape(str(e))}")
            return None, "error"
        except KeyboardInterrupt:
            self.renderer.stop_all()
            if streaming:
                self.renderer.end_message()
            self.console.print("[warn]⏹ interrupted[/warn]")
            partial = "".join(text_acc)
            return (Message(role="assistant", content=partial) if partial else None), "interrupted"

        if completed is not None:
            self._record_usage(completed.usage)
            return completed.message, completed.stop_reason
        # No completion event and no text — treat as empty assistant turn.
        return Message(role="assistant", content="".join(text_acc)), "end_turn"

    def _record_usage(self, usage: dict[str, int]) -> None:
        # "in" stays full-price input only — cached reads bill at ~10% and are
        # counted apart so the two aren't silently averaged together. A session
        # whose 'cached' number stays at 0 is a session paying full price for
        # its whole history every turn.
        self.session_tokens["in"] += usage.get("input_tokens", 0)
        self.session_tokens["out"] += usage.get("output_tokens", 0)
        self.session_tokens["cached"] = self.session_tokens.get("cached", 0) + (
            usage.get("cache_read_input_tokens", 0)
        )
        self.renderer.usage(usage, self.session_tokens)

    def _run_tools(self, tool_calls) -> list[Message] | None:
        results: list[Message] = []
        for call in tool_calls:
            tool = TOOLS_BY_NAME.get(call.name)
            summary = describe_call(call.name, call.arguments)
            self.renderer.tool_call(summary)
            if tool is None:
                results.append(self._tool_error(call, f"Error: unknown tool '{call.name}'"))
                continue
            # Validate required args before dispatch — a malformed call returns a
            # correctable error to the model instead of raising into the handler.
            missing = missing_required_args(tool, call.arguments)
            if missing:
                results.append(self._tool_error(
                    call, f"Error: missing required argument(s): {', '.join(missing)}. "
                    "Provide them and retry."
                ))
                continue
            # Escalating loop-guard: nudge on the 2nd identical command, refuse the
            # 3rd — a warning alone let the model keep thrashing and give up red.
            guard = self._guard_tool_call(call)
            if guard is not None and guard[0] == "refuse":
                self.renderer.nudge(guard[1])
                results.append(self._tool_error(call, f"[goldcomb] {guard[1]}"))
                continue
            if tool.dangerous and not self.auto_approve and call.name not in self.approved_tools:
                decision = self._confirm(summary)
                if decision == "abort":
                    self.console.print("[warn]Aborted.[/warn]")
                    return None
                if decision == "deny":
                    results.append(self._tool_error(call, "User denied this tool call."))
                    continue
                if decision == "always":
                    self.approved_tools.add(call.name)
            self.renderer.update_status(f"Running {call.name}")
            try:
                output = tool.run(call.arguments)
            except Exception as e:  # noqa: BLE001 - surface tool errors to the model
                output = f"Error executing tool: {e}"
            # Printed above the live region — spinner + footer stay pinned.
            self.renderer.update_status("Thinking")
            self.renderer.tool_result(output)
            if guard is not None and guard[0] == "nudge":
                output = f"{output}\n\n[goldcomb] {guard[1]}"
                self.renderer.nudge(guard[1])
            results.append(
                Message(role="tool", content=output, tool_call_id=call.id, name=call.name)
            )
        return results

    def _tool_error(self, call, content: str) -> Message:
        return Message(role="tool", content=content, tool_call_id=call.id, name=call.name)

    # ---- ask the user ------------------------------------------------------

    def _ask_user_impl(self, args: dict) -> str:
        """Backend for the ask_user tool: numbered menus on the terminal.

        Overridden in serve mode, where questions travel over the protocol.
        """
        questions = _valid_questions(args)
        if not questions:
            return "Error: provide a non-empty 'questions' array."
        if not sys.stdin.isatty():
            return (
                "Error: no interactive user is present (non-interactive run). "
                "Proceed with your best judgment and state the assumption you made."
            )
        self.renderer.stop_all()
        answers = []
        for q in questions:
            question = str(q["question"]).strip()
            header = str(q.get("header") or "").strip()
            options = [
                o for o in (q.get("options") or [])
                if isinstance(o, dict) and o.get("label")
            ][:4]
            chip = f"  [dim]({escape(header)})[/dim]" if header else ""
            self.console.print(f"\n[accent]?[/accent] [bold]{escape(question)}[/bold]{chip}")
            for i, o in enumerate(options, 1):
                desc = o.get("description")
                extra = f"  [dim]— {escape(str(desc))}[/dim]" if desc else ""
                self.console.print(f"  [num]{i}[/num]  {escape(str(o['label']))}{extra}")
            if options:
                hint = "number(s), e.g. 1,3" if q.get("multi_select") else "a number"
                self.console.print(f"  [dim]Answer with {hint}, or type your own.[/dim]")
            raw = self._ask_line("  › ").strip()
            answers.append((question, _resolve_answer(raw, options)))
        return "\n".join(f"Q: {q}\nA: {a}" for q, a in answers)

    # ---- sub-agents --------------------------------------------------------

    def _run_subagent(self, args: dict) -> str:
        """Backend for the deploy_agent tool: run one sub-agent to completion.

        The deploying model may name any configured provider and any model;
        both default to the session's current ones (see agents.resolve_target).
        """
        # Every agent gets a human name, however it was created: functional
        # labels become "Ines Vale (retry-worker)" so the board stays legible.
        from .names import humanize
        label = humanize(args.get("label"))
        provider_arg, model_arg = args.get("provider"), args.get("model")
        # Honor the user's pre-configured default model for this agent (set in
        # the app's Agents tab) when the deploying agent didn't pin one — so a
        # deployed agent runs on the model the user chose for it.
        if not model_arg:
            cfg_provider, cfg_model = agents.configured_default(label)
            if cfg_model:
                provider_arg = provider_arg or cfg_provider
                model_arg = cfg_model
        try:
            pname, model = agents.resolve_target(
                self.cfg, provider_arg, model_arg
            )
        except ValueError as e:
            return f"Error: {e}"
        entry = dict(self.cfg.providers[pname])
        entry["api_key"] = self.cfg.resolve_api_key(pname)
        try:
            provider = build_provider(pname, entry)
        except ProviderError as e:
            return f"Error: {e}"

        self.console.print(
            f"[dim]     ⏺ {escape(label)} deployed on {pname} · {model}[/dim]",
            highlight=False,
        )

        def on_event(kind: str, text: str) -> None:
            # Nested progress: dim lines above the live region, and the
            # sub-agent's activity in the spinner label.
            if kind == "tool":
                self.console.print(
                    f"[dim]     ⏺ {escape(label)}: {escape(text)}[/dim]",
                    highlight=False,
                )
            self.renderer.update_status(f"{label} · {text}"[:48])

        settings = self.cfg.settings
        # Lifecycle events for the NDJSON protocol (NEXA-3): bracket the run so
        # GUIs can show the worker in their sidebar. The id is minted here —
        # this path builds no SubAgentHandle — and the parent is this session's
        # live thread id when it exists (created lazily on first save, so it
        # may be None early in a session). No-op on the terminal renderer.
        agent_id = uuid.uuid4().hex[:12]
        self.renderer.subagent_start(
            id=agent_id,
            label=label,
            task=str(args.get("task") or "")[:200],
            parent=self.thread.id if self.thread is not None else None,
            provider=pname,
            model=model,
        )
        # The worker claims scrum tickets under its own label while it runs.
        lead_identity = scrum_mod.CURRENT_AGENT
        scrum_mod.set_agent(label)
        try:
            result = agents.run_subagent(
                provider,
                model,
                args.get("task", ""),
                max_tokens=int(settings.get("max_tokens") or 4096),
                temperature=settings.get("temperature"),
                on_event=on_event,
                label=label,
            )
        except ProviderError as e:
            # Even a failed run leaves a diagnostic record — never empty.
            # (This string goes back to the model as the tool result, not to
            # the console, so no Rich escaping is applied here.)
            err_result = agents.SubAgentResult(
                report="",
                stop_reason="error",
                error=str(e),
            )
            self.renderer.subagent_end(
                id=agent_id,
                label=label,
                stop_reason=err_result.stop_reason,
                iterations=err_result.iterations,
                tool_calls=err_result.tool_calls,
                usage=dict(err_result.usage),
                transcript_path=err_result.transcript_path,
                error=err_result.error,
            )
            return f"Error: sub-agent failed: {e}\n{err_result.diagnostic_footer()}"
        finally:
            scrum_mod.set_agent(lead_identity)
        self.session_tokens["in"] += result.usage["in"]
        self.session_tokens["out"] += result.usage["out"]
        self.renderer.subagent_end(
            id=agent_id,
            label=label,
            stop_reason=result.stop_reason,
            iterations=result.iterations,
            tool_calls=result.tool_calls,
            usage=dict(result.usage),
            transcript_path=result.transcript_path,
            error=result.error,
        )
        header = (
            f"[sub-agent {label} · {pname}/{model} · {result.iterations} step(s), "
            f"{result.tool_calls} tool call(s), "
            f"{result.usage['in']}↑ {result.usage['out']}↓]"
        )
        if result.report:
            return f"{header}\n{result.report}"
        # No final report: fall back to the last assistant message plus a
        # diagnostic footer so the planner can tell stuck from dead.
        body = result.last_assistant_text or "(the sub-agent produced no text at all)"
        return f"{header}\n{body}\n{result.diagnostic_footer()}"

    def _guard_tool_call(self, call) -> tuple[str, str] | None:
        """Detect thrashing. Returns ("nudge"|"refuse", message) or None."""
        args = call.arguments or {}
        if call.name == "run_bash":
            cmd = args.get("command", "")
            self._cmd_counts[cmd] = self._cmd_counts.get(cmd, 0) + 1
            n = self._cmd_counts[cmd]
            if n >= 3:
                return ("refuse",
                        "You have already run this exact command twice with the same "
                        "result. I won't run it a third time — re-read the relevant file, "
                        "state why it is failing, and change your approach.")
            if n == 2:
                return ("nudge",
                        "You've run this exact command twice. If it failed the same way, "
                        "stop repeating it — re-read the file and change your approach.")
        elif call.name == "edit_file":
            path = args.get("path", "")
            self._edit_counts[path] = self._edit_counts.get(path, 0) + 1
            if self._edit_counts[path] >= 3:
                return ("nudge",
                        f"You've edited {path} {self._edit_counts[path]} times. Re-read the "
                        "whole file, then rewrite it in one write_file call instead of more edits.")
        return None

    def _confirm(self, summary: str) -> str:
        # Clear the dynamic region so the y/n prompt sits on a quiet line.
        self.renderer.stop_all()
        self.console.print(
            "[bold]Run this?[/bold] [dim](y=yes, n=no, a=always this tool, q=abort turn)[/dim]"
        )
        try:
            ans = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "abort"
        if ans in ("y", "yes", ""):
            return "yes"
        if ans in ("a", "always"):
            return "always"
        if ans in ("q", "abort"):
            return "abort"
        return "deny"

    # ---- slash commands ----------------------------------------------------

    def handle_command(self, line: str) -> bool:
        """Return False to signal exit, True to continue."""
        parts = line[1:].split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        handler = {
            "help": self.cmd_help,
            "h": self.cmd_help,
            "?": self.cmd_help,
            "setup": self.cmd_setup,
            "wizard": self.cmd_setup,
            "provider": self.cmd_provider,
            "providers": self.cmd_provider,
            "use": self.cmd_use,
            "model": self.cmd_model,
            "models": self.cmd_models,
            "system": self.cmd_system,
            "tools": self.cmd_tools,
            "sudo": self.cmd_sudo,
            "auto": self.cmd_sudo,  # legacy alias
            "render": self.cmd_render,
            "set": self.cmd_set,
            "theme": self.cmd_theme,
            "themes": self.cmd_theme,
            "scrum": self.cmd_scrum,
            "board": self.cmd_scrum,
            "tickets": self.cmd_scrum,
            "memory": self.cmd_memory,
            "clear": self.cmd_clear,
            "compact": self.cmd_compact,
            "history": self.cmd_history,
            "save": self.cmd_save,
            "load": self.cmd_load,
            "config": self.cmd_config,
            "threads": self.cmd_threads,
            "resume": self.cmd_resume,
            "continue": self.cmd_resume,
            "new": self.cmd_new,
            "mode": self.cmd_mode,
            "engine": self.cmd_mode,  # alias
            "exit": lambda a: False,
            "quit": lambda a: False,
            "q": lambda a: False,
        }.get(cmd)
        if handler is None:
            close = difflib.get_close_matches(cmd, COMMANDS, n=1)
            hint = f"  Did you mean [cmd]/{close[0]}[/cmd]?" if close else "  (try /help)"
            self.console.print(f"[err]Unknown command:[/err] /{cmd}{hint}")
            return True
        result = handler(args)
        return False if result is False else True

    def cmd_help(self, args) -> None:
        self.console.print(HELP_TEXT)

    def cmd_provider(self, args) -> None:
        if not args or args[0] == "list":
            self._print_providers()
            return
        sub = args[0]
        rest = args[1:]
        if sub == "add":
            self._provider_add(rest)
        elif sub in ("remove", "rm", "delete"):
            if not rest:
                self.console.print("[err]Usage:[/err] /provider remove <name>")
                return
            self.cfg.remove_provider(rest[0])
            self.console.print(f"[ok]Removed provider '{rest[0]}'.[/ok]")
        elif sub == "use":
            self.cmd_use(rest)
        elif sub == "set":
            if len(rest) < 3:
                self.console.print("[err]Usage:[/err] /provider set <name> <field> <value>")
                return
            name, field, value = rest[0], rest[1], " ".join(rest[2:])
            try:
                self.cfg.set_provider_field(name, field, value)
                self.console.print(f"[ok]Set {name}.{field}.[/ok]")
            except KeyError:
                self.console.print(f"[err]No such provider:[/err] {name}")
        elif sub == "models":
            self.cmd_models(rest)
        else:
            self.console.print(f"[err]Unknown subcommand:[/err] /provider {sub}")

    def _provider_add(self, rest) -> None:
        # No args → the guided menu.
        if not rest:
            return self.cmd_setup([])
        first = rest[0]
        fkey = first.lower()
        second_is_type = len(rest) >= 2 and normalize_type(rest[1]) in PROVIDER_TYPES
        # `/provider add openrouter [key]` — first token is a known preset.
        if fkey in PRESETS_BY_KEY and not second_is_type:
            key = rest[1] if len(rest) > 1 else None
            return self._add_from_preset(PRESETS_BY_KEY[fkey], name=None, api_key=key)
        # A single unrecognized token → nudge toward the wizard.
        if len(rest) < 2:
            self.console.print(
                f"[warn]Not sure how to add '{first}'.[/warn]  "
                "Run [cmd]/setup[/cmd] for a menu, or "
                r"[dim]/provider add <name> <type>[/dim]."
            )
            return
        # Explicit `<name> <type> [key] [url]`.
        name = rest[0]
        preset = PRESETS_BY_KEY.get(rest[1].lower())
        ptype = normalize_type(rest[1])
        if ptype not in PROVIDER_TYPES:
            if preset:  # they used a preset key where a type was expected
                return self._add_from_preset(
                    preset, name=name, api_key=rest[2] if len(rest) > 2 else None
                )
            self.console.print(
                f"[err]Unknown type '{rest[1]}'.[/err]  "
                f"Known: {', '.join(sorted(PROVIDER_TYPES))}.  Or run [cmd]/setup[/cmd]."
            )
            return
        api_key = rest[2] if len(rest) > 2 else None
        base_url = rest[3] if len(rest) > 3 else (preset.base_url if preset else None)
        if ptype == "openai-compatible" and not base_url and sys.stdin.isatty():
            base_url = self._ask_line(
                "Base URL (e.g. https://openrouter.ai/api/v1): "
            ).strip() or None
        if not api_key and sys.stdin.isatty():
            api_key = self._ask_secret(
                f"API key for '{name}' (blank to use env var): "
            ).strip() or None
        self.cfg.add_provider(name, ptype, api_key=api_key, base_url=base_url or None)
        self._announce_added(name, ptype, preset.default_model if preset else "")

    # ---- guided setup ------------------------------------------------------

    def cmd_setup(self, args) -> None:
        """Interactive wizard: pick a provider from a menu, paste a key, go."""
        self.console.rule("[bold]Add a provider[/bold]")
        options = [(p.key, p.label) for p in PRESETS]
        options.append(("custom", "Custom — any OpenAI-compatible endpoint (you give a URL)"))
        choice = self._menu("Which provider would you like to use?", options)
        if choice is None:
            self.console.print("[dim]Cancelled.[/dim]")
            return
        if choice == "custom":
            return self._add_custom()
        self._add_from_preset(PRESETS_BY_KEY[choice], name=None, api_key=None)

    def _add_from_preset(self, preset: Preset, name: str | None, api_key: str | None) -> None:
        name = name or self._unique_name(preset.key)
        if api_key is None and preset.needs_key:
            env_key = preset.env and os.environ.get(preset.env)
            if env_key:
                self.console.print(
                    f"[dim]Found {preset.env} in your environment — I'll use that key.[/dim]"
                )
            elif sys.stdin.isatty():
                if preset.key_url:
                    self.console.print(f"[dim]Need a key? Get one at:[/dim] {preset.key_url}")
                api_key = self._ask_secret(
                    f"Paste your {preset.label.split(' — ')[0]} API key: "
                ).strip() or None
                if not api_key:
                    self.console.print(
                        "[warn]No key entered.[/warn] You can add it later: "
                        f"[dim]/provider set {name} api_key <key>[/dim]"
                    )
        self.cfg.add_provider(name, preset.type, api_key=api_key, base_url=preset.base_url)
        if preset.note:
            self.console.print(f"[dim]{preset.note}[/dim]")
        self._announce_added(name, preset.type, preset.default_model)

    def _add_custom(self) -> None:
        base_url = self._ask_line("Base URL (e.g. http://localhost:8000/v1): ").strip()
        if not base_url:
            self.console.print("[warn]Cancelled — a base URL is required.[/warn]")
            return
        name = (
            self._ask_line("Name for this provider [custom]: ").strip()
            or self._unique_name("custom")
        )
        api_key = self._ask_secret(
            "API key (leave blank if the endpoint needs none): "
        ).strip() or None
        self.cfg.add_provider(name, "openai-compatible", api_key=api_key, base_url=base_url)
        self._announce_added(name, "openai-compatible", "")

    def _announce_added(self, name: str, ptype: str, default_model: str) -> None:
        # Give a fresh provider a sensible model if one wasn't set on add.
        if self.cfg.current_provider == name and default_model and not self.cfg.current_model:
            self.cfg.set_model(default_model)
        self.console.print(f"[ok]✓ Added '{name}'[/ok] [dim]({ptype})[/dim]")
        if self.cfg.current_provider == name:
            model = self.cfg.current_model or "[warn]no model yet[/warn]"
            self.console.print(
                f"[ok]Now using[/ok] [accent2]{name}[/accent2] / [accent]{model}[/accent]"
            )

        # Offer to fetch the live model list — only when a human is present and
        # we have some way to authenticate (a key, or a local keyless endpoint).
        base_url = self.cfg.providers.get(name, {}).get("base_url", "") or ""
        is_local = "localhost" in base_url or "127.0.0.1" in base_url
        can_list = self.cfg.resolve_api_key(name) is not None or is_local
        if sys.stdin.isatty() and self.cfg.current_provider == name and can_list:
            ans = self._ask_line("Fetch this provider's model list now? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                self._pick_model_interactive(name)
        elif not self.cfg.current_model:
            self.console.print(
                "[dim]Pick a model with /models, or set one with /model <name>.[/dim]"
            )

    def _unique_name(self, base: str) -> str:
        if base not in self.cfg.providers:
            return base
        i = 2
        while f"{base}-{i}" in self.cfg.providers:
            i += 1
        return f"{base}-{i}"

    def _menu(self, title: str, options: list[tuple[str, str]]) -> str | None:
        self.console.print(f"[bold]{title}[/bold]")
        for i, (_key, label) in enumerate(options, 1):
            self.console.print(f"  [num]{i:>2}[/num]  {label}")
        raw = self._ask_line("Enter a number or name (blank to cancel): ").strip().lower()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
            self.console.print("[err]That number isn't on the list.[/err]")
            return None
        keys = [k for k, _ in options]
        if raw in keys:
            return raw
        matches = [k for k, label in options if k.startswith(raw) or label.lower().startswith(raw)]
        if len(matches) == 1:
            return matches[0]
        self.console.print("[err]Didn't recognize that choice.[/err]")
        return None

    def _pick_model_interactive(self, name: str) -> None:
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        provider = build_provider(name, entry)
        self.console.print("[dim]Fetching models…[/dim]")
        try:
            models = provider.list_models()
        except ProviderError as e:
            self.console.print(
                f"[err]{escape(str(e))}[/err]  [dim]Set one manually with /model <name>.[/dim]"
            )
            return
        if not models:
            self.console.print("[dim]No models returned. Set one with /model <name>.[/dim]")
            return
        self.cfg.cache_models(name, models)
        self._last_models = models
        for i, m in enumerate(models[:40], 1):
            self.console.print(f"  [num]{i:>2}[/num]  {m}")
        if len(models) > 40:
            self.console.print(f"  [dim]… +{len(models) - 40} more (see /models)[/dim]")
        raw = self._ask_line(
            f"Pick a model by number or name [keep {self.cfg.current_model or 'none'}]: "
        ).strip()
        if not raw:
            return
        sel = self._resolve_model_choice(raw)
        if sel:
            self.cfg.set_model(sel)
            self.console.print(f"[ok]Model set to[/ok] {sel}")

    def _resolve_model_choice(self, raw: str) -> str | None:
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(self._last_models):
                return self._last_models[idx]
            self.console.print("[err]No model with that number — run /models first.[/err]")
            return None
        if raw in self._last_models:
            return raw
        matches = [m for m in self._last_models if raw.lower() in m.lower()]
        if len(matches) == 1:
            return matches[0]
        # Otherwise accept it verbatim (lets you type a model not in the list).
        return raw

    def _print_providers(self) -> None:
        if not self.cfg.providers:
            self.console.print(
                "[warn]No providers configured.[/warn] Run [cmd]/setup[/cmd] for a guided menu."
            )
            return
        table = Table(
            show_header=True, header_style=f"bold {theme.ACCENT}",
            box=box.SIMPLE_HEAD, border_style="dim",
        )
        table.add_column("", width=2)
        table.add_column("name")
        table.add_column("type")
        table.add_column("key")
        table.add_column("base_url")
        for name, entry in self.cfg.providers.items():
            marker = "[cur]→[/cur]" if name == self.cfg.current_provider else ""
            has_key = "[ok]✓[/ok]" if self.cfg.resolve_api_key(name) else "[err]✗[/err]"
            table.add_row(marker, name, entry.get("type", "?"), has_key, entry.get("base_url", ""))
        self.console.print(table)

    def cmd_use(self, args) -> None:
        if not args:
            self.console.print(r"[err]Usage:[/err] /use <provider> \[model]")
            return
        name = args[0]
        model = args[1] if len(args) > 1 else None
        try:
            self.cfg.use_provider(name, model)
        except KeyError:
            self.console.print(f"[err]No such provider:[/err] {name}")
            return
        self.console.print(
            f"[ok]Using[/ok] {name} / {self.cfg.current_model or '(no model — set with /model)'}"
        )

    def cmd_model(self, args) -> None:
        if not args:
            self.console.print(
                f"Current: [accent2]{self.cfg.current_provider}[/accent2] / "
                f"[accent]{self.cfg.current_model or '(none)'}[/accent]"
            )
            if self.cfg.current_provider:
                self.console.print(
                    "[dim]Set with /model <name>, or /models to pick from a numbered list.[/dim]"
                )
            return
        if args[0] == "list":
            provider = self.cfg.current_provider
            ptype = self.cfg.providers.get(provider or "", {}).get("type", "")
            cached = self.cfg.models_for(provider)
            models = cached or default_models_for(ptype)
            if models:
                if cached:
                    self.console.print(
                        f"Models for [accent2]{provider}[/accent2] "
                        "[dim](cached from the API)[/dim]:"
                    )
                else:
                    self.console.print("Common models for this provider type:")
                for m in models:
                    marker = " [cur]← current[/cur]" if m == self.cfg.current_model else ""
                    self.console.print(f"  {m}{marker}")
                if not cached:
                    self.console.print(
                        "[dim]Run /models to fetch the full live list from the API.[/dim]"
                    )
            else:
                self.console.print("[dim]No known models; run /models to fetch them.[/dim]")
            return
        choice = args[0]
        # `/model 3` picks the 3rd entry from the last /models listing.
        if choice.isdigit() and self._last_models:
            sel = self._resolve_model_choice(choice)
            if sel is None:
                return
        else:
            sel = choice
        try:
            self.cfg.set_model(sel)
            self.console.print(f"[ok]Model set to[/ok] {sel}")
        except ValueError as e:
            self.console.print(f"[err]{escape(str(e))}[/err]")

    def cmd_models(self, args) -> None:
        # Args can name a provider, a filter substring, and/or "all".
        name = self.cfg.current_provider
        filt: str | None = None
        show_all = False
        for a in args:
            if a.lower() == "all":
                show_all = True
            elif a in self.cfg.providers:
                name = a
            else:
                filt = a
        if not name:
            self.console.print("[err]No provider selected.[/err] Run [cmd]/setup[/cmd].")
            return
        if name not in self.cfg.providers:
            self.console.print(f"[err]No such provider:[/err] {name}")
            return
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        provider = build_provider(name, entry)
        self.console.print(f"[dim]Querying {name} for models…[/dim]")
        try:
            models = provider.list_models()
        except ProviderError as e:
            self.console.print(f"[err]{escape(str(e))}[/err]")
            return
        # Price decoration: only for openai-compatible endpoints that publish
        # pricing in their /models payload (OpenRouter does; others just don't
        # answer with it and we show nothing).
        prices: dict[str, tuple[float, float]] = {}
        if entry.get("type") == "openai-compatible" and entry.get("base_url"):
            prices = fetch_prices(entry["base_url"], entry["api_key"])
        # Cache the FULL live catalog so /model list and completion have it
        # offline, even though we may show a trimmed view below.
        self.cfg.cache_models(name, models)
        # Hide non-chat OpenAI models by default (embeddings, tts, image, …).
        hidden = 0
        ptype = self.cfg.providers[name].get("type", "")
        if not show_all and not filt and ptype == "openai":
            chat = chat_models_only(models)
            hidden = len(models) - len(chat)
            if chat:
                models = chat
        if filt:
            models = [m for m in models if filt.lower() in m.lower()]
            if not models:
                self.console.print(f"[warn]No models matching '{filt}'.[/warn]")
                return
        self._last_models = models
        for i, m in enumerate(models, 1):
            marker = " [cur]← current[/cur]" if m == self.cfg.current_model else ""
            price = f" [dim]· {format_price(prices[m])}[/dim]" if m in prices else ""
            self.console.print(f"  [num]{i:>3}[/num]  {m}{price}{marker}")
        footer = f"[dim]{len(models)} models — set one with[/dim] [cmd]/model <number>[/cmd]"
        if hidden:
            footer += f" [dim]· {hidden} non-chat hidden — [/dim][cmd]/models all[/cmd]"
        self.console.print(footer)

    def cmd_system(self, args) -> None:
        if not args:
            cur = self.cfg.settings.get("system_prompt")
            self.console.print(f"System prompt: {cur or '[dim](default agent prompt)[/dim]'}")
            return
        if args[0] == "clear":
            self.cfg.set_setting("system_prompt", None)
            self.console.print("[ok]Cleared custom system prompt.[/ok]")
            return
        self.cfg.set_setting("system_prompt", " ".join(args))
        self.console.print("[ok]System prompt set.[/ok]")

    def cmd_tools(self, args) -> None:
        if args and args[0] in ("on", "off"):
            self.cfg.set_setting("tools_enabled", args[0] == "on")
        elif args:
            self.console.print(r"[err]Usage:[/err] /tools \[on|off]")
            return
        state = "on" if self.cfg.settings.get("tools_enabled") else "off"
        self.console.print(f"Tools: [accent2]{state}[/accent2]  ({', '.join(TOOLS_BY_NAME)})")

    def cmd_sudo(self, args) -> None:
        if args and args[0] in ("on", "off"):
            self.auto_approve = args[0] == "on"
        else:
            self.auto_approve = not self.auto_approve
        if self.auto_approve:
            self.console.print(
                "Sudo mode: [accent2]on[/accent2]  "
                "[warn](tool calls run without confirmation)[/warn]"
            )
        else:
            self.console.print("Sudo mode: [accent2]off[/accent2]")

    def cmd_mode(self, args) -> None:
        """Switch execution engine: native (goldcomb's tool loop, any provider)
        or claude (delegate the turn to the Claude Agent SDK / Claude Code)."""
        from .engines import ENGINES
        from .engines.claude import sdk_available
        from .providers import normalize_type

        if not args:
            self._show_mode()
            return
        choice = args[0].strip().lower()
        if choice not in ENGINES:
            self.console.print(
                r"[err]Usage:[/err] /mode \[" + "|".join(ENGINES) + "]")
            return
        self.engine = choice
        self.cfg.set_setting("engine", choice)
        if choice == "claude":
            if not sdk_available():
                self.console.print(
                    "[warn]Claude mode set, but the Claude Agent SDK isn't "
                    "installed yet:[/warn]\n  [accent]pip install claude-agent-sdk[/accent]")
            pname = self.cfg.current_provider or ""
            ptype = normalize_type(self.cfg.providers.get(pname, {}).get("type", ""))
            if ptype != "anthropic":
                self.console.print(
                    f"[dim]Note: claude mode always runs the Claude Code (Anthropic) "
                    f"harness; the active '{pname}' provider is only used for its key "
                    f"if it's Anthropic. Auth falls back to your Claude Code login / "
                    f"ANTHROPIC_API_KEY.[/dim]")
            self.console.print(
                "Engine: [accent2]claude[/accent2]  "
                "[dim](turns run via the Claude Code harness — its own tools, "
                "auto-approved)[/dim]")
        else:
            self.console.print(
                "Engine: [accent2]native[/accent2]  "
                "[dim](goldcomb's own tool loop, any provider)[/dim]")

    def _show_mode(self) -> None:
        from .engines.claude import sdk_available
        note = ""
        if self.engine == "claude" and not sdk_available():
            note = "  [warn](SDK not installed — pip install claude-agent-sdk)[/warn]"
        self.console.print(
            f"Engine: [accent2]{self.engine}[/accent2]{note}\n"
            "[dim]native = goldcomb's tool loop (any provider); "
            "claude = the Claude Code harness (Anthropic). "
            r"Switch with /mode \[native|claude].[/dim]")

    def cmd_render(self, args) -> None:
        if args and args[0] in ("on", "off"):
            self.cfg.set_setting("render_markdown", args[0] == "on")
        elif args:
            self.console.print(r"[err]Usage:[/err] /render \[on|off]")
            return
        self.renderer.markdown = bool(self.cfg.settings.get("render_markdown"))
        state = "on" if self.renderer.markdown else "off"
        self.console.print(
            f"Markdown rendering: [accent2]{state}[/accent2]  "
            "[dim](off = plain streamed text)[/dim]"
        )

    def _set_theme(self, name: str, save: bool) -> None:
        theme.apply_theme(name)
        self._refresh_prompt_style()
        if save:
            self.cfg.set_setting("theme", name)
        self.console.print(f"[ok]Theme:[/ok] {name}")

    def cmd_theme(self, args) -> None:
        if args and args[0] in ("list", "ls"):
            for name in THEMES:
                marker = " [cur]← current[/cur]" if name == theme.CURRENT_THEME else ""
                self.console.print(f"  {name}{marker}")
            return
        if args:
            name = args[0].lower()
            if name not in THEMES:
                self.console.print(
                    f"[err]Unknown theme:[/err] {name}  "
                    f"[dim]Known: {', '.join(THEMES)}[/dim]"
                )
                return
            self._set_theme(name, save=True)
            return
        self.console.print(f"Current theme: [accent]{theme.CURRENT_THEME}[/accent]")
        if not (sys.stdin.isatty() and self.console.is_terminal):
            self.console.print(
                f"[dim]Pick one with /theme <name> — known: {', '.join(THEMES)}[/dim]"
            )
            return
        self.console.print("[dim]↑/↓ to preview · enter to apply · esc to cancel[/dim]")
        picked = pick_theme(self.console, theme.CURRENT_THEME)
        if picked and picked != theme.CURRENT_THEME:
            self._set_theme(picked, save=True)
        else:
            self.console.print("[dim]Kept the current theme.[/dim]")

    def _refresh_prompt_style(self) -> None:
        """Re-skin the prompt_toolkit chrome (bottom toolbar, › prompt) so a
        /theme switch takes effect at the very next prompt, not next launch."""
        if self._pt_style is None:
            return
        try:
            from prompt_toolkit.styles import Style as PtStyle
        except ImportError:  # pragma: no cover - pt was present at startup
            return
        self._pt_style = PtStyle(_pt_style_rules())

    def cmd_set(self, args) -> None:
        if len(args) < 2:
            self.console.print("[err]Usage:[/err] /set <max_tokens|temperature> <value>")
            self.console.print(
                f"[dim]max_tokens={self.cfg.settings.get('max_tokens')} "
                f"temperature={self.cfg.settings.get('temperature')}[/dim]"
            )
            return
        key, value = args[0], args[1]
        if key == "max_tokens":
            self.cfg.set_setting("max_tokens", int(value))
        elif key == "temperature":
            self.cfg.set_setting("temperature", None if value in ("none", "off") else float(value))
        else:
            self.console.print(f"[err]Unknown setting:[/err] {key}")
            return
        self.console.print(f"[ok]{key} = {value}[/ok]")

    def cmd_memory(self, args) -> None:
        """Show this agent's private memory file (/memory)."""
        from . import memory as memory_mod
        current = memory_mod.read_memory()
        path = memory_mod.memory_path()
        if current:
            self.console.print(f"[dim]{path}[/dim]\n{current}")
        else:
            self.console.print(
                f"[dim]No memory yet at {path} — the agent builds it with "
                "the memory tool.[/dim]")

    def cmd_scrum(self, args) -> None:
        """Per-project ticket tracking: /scrum on|off|show (or bare /scrum)."""
        sub = args[0].lower() if args else ""
        if sub == "on":
            self.console.print(f"[ok]{escape(scrum_mod.enable())}[/ok]")
            self.console.print(
                "[dim]The scrum tool is now offered to the model in this "
                "project — ask it to plan work as tickets.[/dim]"
            )
        elif sub == "off":
            self.console.print(f"[warn]{escape(scrum_mod.disable())}[/warn]")
        elif sub in ("", "show", "status"):
            if not scrum_mod.is_enabled():
                self.console.print(
                    "Scrum tracking: [accent2]off[/accent2] for this project.  "
                    "[dim]Enable with[/dim] [cmd]/scrum on[/cmd]"
                )
                return
            self.console.print(escape(scrum_mod.scrum({"action": "show"})))
        else:
            self.console.print(r"[err]Usage:[/err] /scrum \[on|off|show]")

    def cmd_clear(self, args) -> None:
        self.messages.clear()
        # Detach from the saved thread so the next turn opens a fresh one rather
        # than overwriting the old thread with an emptied conversation.
        self.thread = None
        clear_viewport(self.console)
        self.console.print("[ok]Conversation cleared.[/ok]")

    # -- compaction ----------------------------------------------------------

    def _render_transcript(self, messages: list[Message]) -> str:
        """The conversation as plain text for the summarizer. Tool calls and
        results are folded in briefly so the summary knows what was done."""
        lines: list[str] = []
        for m in messages:
            if m.role == "tool":
                body = " ".join((m.content or "").split())
                lines.append(f"[tool result] {body[:600]}")
                continue
            who = "user" if m.role == "user" else "assistant"
            if m.content:
                lines.append(f"{who}: {m.content}")
            for call in m.tool_calls:
                lines.append(f"[assistant called {call.name} {call.arguments}]")
        return "\n".join(lines)

    def compact_conversation(self, provider: Provider) -> dict:
        """Summarize the conversation and replace history with the summary, so
        the next turn carries the gist at a fraction of the tokens.

        Returns a result dict — ``{ok, before, after}`` on success, or
        ``{ok: False, reason}`` when there's nothing to compact or the summary
        came back empty. Never raises for the "too short" / "empty" cases;
        ProviderError propagates to the caller (both callers catch it).
        """
        original = list(self.messages)
        # Below a couple of turns there's nothing to gain — the summary would
        # cost more than it saves.
        if len(original) < 4:
            return {"ok": False, "reason": "too-short", "before": len(original)}
        self.renderer.start_status("Compacting")
        try:
            request = [Message(role="user", content=self._render_transcript(original))]
            completed: Completed | None = None
            for ev in provider.stream(
                request,
                model=self.cfg.current_model or "",
                system=COMPACT_SYSTEM,
                tools=None,
                max_tokens=int(self.cfg.settings.get("max_tokens") or 4096),
                temperature=self.cfg.settings.get("temperature"),
            ):
                if isinstance(ev, Completed):
                    completed = ev
        finally:
            self.renderer.stop_status()
        summary = (completed.message.content if completed else "").strip()
        if not summary:
            return {"ok": False, "reason": "empty-summary", "before": len(original)}
        if completed:
            self._record_usage(completed.usage)
        self.messages = [Message(role="user", content=COMPACT_PREFIX + summary)]
        return {"ok": True, "before": len(original), "after": len(self.messages),
                "summary": summary}

    def cmd_compact(self, args) -> None:
        try:
            provider = self.get_provider()
        except ProviderError as e:
            self.console.print(f"[err]{escape(str(e))}[/err]")
            return
        try:
            result = self.compact_conversation(provider)
        except ProviderError as e:
            self.console.print(f"[err]Compaction failed:[/err] {escape(str(e))}")
            return
        if not result["ok"]:
            if result["reason"] == "too-short":
                self.console.print(
                    "[dim]Nothing to compact yet — the conversation is short.[/dim]")
            else:
                self.console.print(
                    "[warn]Compaction produced no summary; history unchanged.[/warn]")
            return
        self._autosave()
        self.console.print(
            f"[ok]Compacted[/ok] {result['before']} messages → a summary "
            f"([dim]saved; the conversation continues from here[/dim])")

    def cmd_history(self, args) -> None:
        self.console.print(f"[dim]{len(self.messages)} messages in this conversation[/dim]")
        role_styles = {"user": "accent2", "assistant": "accent", "tool": "dim"}
        for m in self.messages:
            role = m.role
            preview = (m.content or "").replace("\n", " ")[:80]
            if m.tool_calls:
                preview += f" [{', '.join(t.name for t in m.tool_calls)}]"
            style = role_styles.get(role, "bold")
            self.console.print(f"  [{style}]{role}:[/] {escape(preview)}")

    def cmd_save(self, args) -> None:
        path = Path(args[0]) if args else Path("goldcomb-session.json")
        data = {
            "provider": self.cfg.current_provider,
            "model": self.cfg.current_model,
            "messages": [m.to_dict() for m in self.messages],
        }
        try:
            path.write_text(json.dumps(data, indent=2))
        except OSError as e:
            self.console.print(f"[err]Error saving:[/err] {escape(str(e))}")
            return
        self.console.print(f"[ok]Saved {len(self.messages)} messages to {path}[/ok]")

    def cmd_load(self, args) -> None:
        path = Path(args[0]) if args else Path("goldcomb-session.json")
        if not path.exists():
            self.console.print(f"[err]Not found:[/err] {path}")
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            self.console.print(f"[err]Error loading:[/err] {escape(str(e))}")
            return
        self.messages = [Message.from_dict(m) for m in data.get("messages", [])]
        self.console.print(f"[ok]Loaded {len(self.messages)} messages from {path}[/ok]")

    def cmd_config(self, args) -> None:
        self.console.print(f"Config file: [dim]{self.cfg.path}[/dim]")
        self.console.print(
            f"Current: [accent2]{self.cfg.current_provider}[/accent2] / "
            f"[accent]{self.cfg.current_model}[/accent]"
        )
        self._print_providers()

    # ---- threads -----------------------------------------------------------

    def cmd_threads(self, args) -> None:
        """List saved threads for this project (most recent first)."""
        thread_list = threads.list_threads()
        self._last_threads = thread_list
        if not thread_list:
            self.console.print(
                "[dim]No saved threads in this project yet. Start chatting — "
                "it autosaves; resume later with[/dim] [cmd]/resume[/cmd]."
            )
            return
        table = Table(
            show_header=True, header_style=f"bold {theme.ACCENT}",
            box=box.SIMPLE_HEAD, border_style="dim",
        )
        table.add_column("", width=3, justify="right")
        table.add_column("thread")
        table.add_column("msgs", justify="right")
        table.add_column("updated")
        table.add_column("title")
        for i, t in enumerate(thread_list, 1):
            marker = "[cur]→[/cur]" if self.thread and t.id == self.thread.id else f"[num]{i}[/num]"
            table.add_row(
                marker, t.id, str(t.message_count),
                t.updated.replace("T", " ")[:19], t.title,
            )
        self.console.print(table)
        self.console.print(
            "[dim]Resume with[/dim] [cmd]/resume <number|id>[/cmd][dim], "
            "or[/dim] [cmd]/new[/cmd] [dim]for a fresh one.[/dim]"
        )

    def cmd_resume(self, args) -> None:
        """Resume a thread by number (from /threads), id/prefix, or 'latest'."""
        if not args:
            # No argument: resume the most recent, but show the list first so the
            # user can see what else is there.
            latest = threads.latest_thread()
            if latest is None:
                self.console.print("[dim]No threads to resume yet.[/dim]")
                return
            self.cmd_threads([])
            raw = self._ask_line(
                f"Resume which? [number/id, blank = latest ({latest.id})]: "
            ).strip()
            target = self._resolve_thread_choice(raw) if raw else latest
        else:
            target = self._resolve_thread_choice(args[0])
        if target is None:
            return
        self._adopt_thread(target)

    def cmd_new(self, args) -> None:
        """Start a fresh thread, leaving the previous one saved."""
        self.messages.clear()
        self.thread = None
        clear_viewport(self.console)
        self.console.print("[ok]Started a new thread.[/ok] [dim](previous one is saved)[/dim]")

    def _resolve_thread_choice(self, raw: str) -> threads.Thread | None:
        raw = raw.strip()
        if raw in ("latest", "last"):
            t = threads.latest_thread()
            if t is None:
                self.console.print("[dim]No threads to resume.[/dim]")
            return t
        if raw.isdigit() and self._last_threads:
            idx = int(raw) - 1
            if 0 <= idx < len(self._last_threads):
                # Re-load fresh from disk in case it changed since /threads.
                return threads.load_thread(self._last_threads[idx].id) or self._last_threads[idx]
            self.console.print("[err]No thread with that number — run /threads first.[/err]")
            return None
        t = threads.load_thread(raw)
        if t is None:
            self.console.print(
                f"[err]No thread matching[/err] '{raw}'. [dim]Run /threads to list them.[/dim]"
            )
        return t

    # ---- input helpers -----------------------------------------------------

    def _ask_line(self, prompt: str) -> str:
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    def _ask_secret(self, prompt: str) -> str:
        try:
            return getpass(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""


HELP_TEXT = r"""[bold]goldcomb commands[/bold]

[heading]Providers[/heading]
  [cmd]/setup[/cmd]                                 guided menu — pick a provider, paste a key
  [cmd]/provider add[/cmd] <preset|name type> …     add a provider (e.g. /provider add kimi)
  [cmd]/provider list[/cmd]                         show configured providers
  [cmd]/provider remove[/cmd] <name>                delete a provider
  [cmd]/provider set[/cmd] <name> <field> <value>   edit api_key / base_url

[heading]Models[/heading]
  [cmd]/use[/cmd] <provider> \[model]                switch active provider
  [cmd]/model[/cmd] \[name|number]                   set the model (number from /models)
  [cmd]/model list[/cmd]                            known models for this provider type
  [cmd]/models[/cmd] \[provider] \[filter]            numbered live model list from the API

[heading]Conversation[/heading]
  [cmd]/system[/cmd] \[prompt|clear]                 show/set system prompt
  [cmd]/clear[/cmd]                                 reset the conversation
  [cmd]/compact[/cmd]                               summarize history to shrink context
  [cmd]/history[/cmd]                               list messages so far
  [cmd]/save[/cmd] \[path] · [cmd]/load[/cmd] \[path]          export/import a session file

[heading]Threads[/heading]
  [cmd]/threads[/cmd]                               list saved threads for this project
  [cmd]/resume[/cmd] \[number|id]                    resume a thread (blank = pick from a list)
  [cmd]/new[/cmd]                                   start a fresh thread

[heading]Settings[/heading]
  [cmd]/tools[/cmd] \[on|off]                        toggle file/shell tools
  [cmd]/sudo[/cmd] \[on|off]                         run tool calls without confirmation
  [cmd]/render[/cmd] \[on|off]                       markdown vs plain streaming
  [cmd]/theme[/cmd] \[name]                          pick a color theme (arrow-key preview)
  [cmd]/set[/cmd] max_tokens|temperature <value>    tune generation
  [cmd]/scrum[/cmd] \[on|off|show]                   per-project ticket tracking
  [cmd]/config[/cmd]                                show config location
  [cmd]/help[/cmd]  ·  [cmd]/exit[/cmd]

  [dim]presets: anthropic, openai, gemini, openrouter, groq, deepseek, kimi,
  mistral, together, ollama, lmstudio  —  e.g.  /provider add kimi[/dim]

Type a message to chat. Ctrl-C interrupts a response; Ctrl-D or /exit quits."""


# ---- /theme: live-preview picker -------------------------------------------


def _theme_preview(console: Console, name: str, selected: bool) -> None:
    """Render one theme's swatch in the pick-list, in that theme's own colors."""
    spec = THEMES[name]
    a, a2 = spec["accent"], spec["accent2"]
    bg, fg, em = spec["tb_bg"], spec["tb_fg"], spec["tb_em"]
    arrow = f"[{a}]❯[/]" if selected else " "
    bar = Text.assemble(
        (" openai · gpt-4o ", f"bold {em} on {bg}"),
        ("  ", f"on {bg}"),
        ("⬆1.2k ⬇340 · ctx ~2k · tools ", f"{fg} on {bg}"),
    )
    console.print(
        f" {arrow} [{a}]{name:<9}[/] [dim]{spec['blurb']}[/dim]", highlight=False
    )
    console.print(
        f"     [{a}]⏺[/] [dim]assistant message[/dim]   "
        f"[{a2}]⏺[/] [bold]$ run_bash ls[/bold]   "
        f"[dim {a2}]  ⎿  [/][dim]result[/dim]",
        highlight=False,
    )
    console.print("     ", bar, highlight=False)


def pick_theme(console: Console, current: str) -> str | None:
    """Interactive theme chooser: ↑/↓ moves (re-rendering the list in that
    theme's colors as a live preview), enter applies, esc/q cancels.

    The theme is *previewed* by applying it process-wide while drawing, so the
    surrounding UI repaints in the candidate's colors too; the original theme
    is restored on cancel. Returns the chosen name, or None if cancelled.
    """
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - windows
        return None

    names = list(THEMES)
    idx = names.index(current) if current in names else 0
    block_h = 3
    total_h = block_h * len(names)
    state = {"first": True}

    def draw() -> None:
        # Preview = actually apply the theme, then paint the list in it.
        theme.apply_theme(names[idx])
        if state["first"]:
            state["first"] = False
        else:
            console.file.write(f"\x1b[{total_h}F")  # cursor up over the old list
        for i, name in enumerate(names):
            _theme_preview(console, name, selected=(i == idx))
        console.file.flush()

    def read_key() -> str:
        ch = sys.stdin.read(1)
        if ch == "\x1b":  # escape sequence (arrow key) or a bare Esc
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down"}.get(seq, "esc")
        if ch in ("\r", "\n"):
            return "enter"
        if ch in ("q", "Q"):
            return "esc"
        if ch == "k":
            return "up"
        if ch == "j":
            return "down"
        return ""

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # key-by-key input, ISIG kept so Ctrl-C still interrupts
        draw()
        while True:
            key = read_key()
            if key == "up":
                idx = (idx - 1) % len(names)
                draw()
            elif key == "down":
                idx = (idx + 1) % len(names)
                draw()
            elif key == "enter":
                console.file.write("\n")
                console.file.flush()
                return names[idx]
            elif key == "esc":
                theme.apply_theme(current)  # stop previewing
                console.file.write("\n")
                console.file.flush()
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---- REPL & entry point -----------------------------------------------------


def _fmt_k(n: int) -> str:
    if n >= 10000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _valid_questions(args: dict) -> list[dict]:
    """The usable questions from an ask_user call: dicts with a non-empty
    question text, capped at 4."""
    raw = args.get("questions")
    if not isinstance(raw, list):
        return []
    return [
        q for q in raw
        if isinstance(q, dict) and str(q.get("question", "")).strip()
    ][:4]


def _resolve_answer(raw: str, options: list[dict]) -> str:
    """Map a typed reply to option labels when it's numbers, else keep it."""
    if not raw:
        return "(no answer — decide yourself)"
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if options and tokens and all(
        t.isdigit() and 1 <= int(t) <= len(options) for t in tokens
    ):
        return ", ".join(str(options[int(t) - 1]["label"]) for t in tokens)
    return raw


def _pt_style_rules() -> list[tuple[str, str]]:
    """prompt_toolkit style rules built from the *current* theme."""
    return [
        ("prompt", f"bold {theme.ACCENT}"),
        # ``noreverse`` overrides prompt_toolkit's reverse-video default.
        ("bottom-toolbar", f"noreverse bg:{theme.TB_BG} {theme.TB_FG}"),
        ("tb-model", f"bold {theme.TB_EM}"),
        ("tb-flag", theme.ACCENT2),
        ("tb-flag-warn", theme.WARN),
    ]


def _build_prompt_session(app: App):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import NestedCompleter
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style as PtStyle
    except ImportError:
        return None
    from html import escape as html_escape

    from prompt_toolkit.styles import DynamicStyle

    # The style object is swapped out by _refresh_prompt_style on /theme, and
    # DynamicStyle re-reads it every render, so the chrome restyles live.
    app._pt_style = PtStyle(_pt_style_rules())

    def completer_dict() -> dict:
        provider_names = {n: None for n in app.cfg.providers}
        preset_keys = {p.key: None for p in PRESETS}
        cur = app.cfg.current_provider or ""
        model_list = (
            app._last_models
            or app.cfg.models_for(cur)
            or default_models_for(app.cfg.providers.get(cur, {}).get("type", ""))
        )
        model_names = {m: None for m in model_list}
        return {
            "/help": None,
            "/setup": None,
            "/provider": {
                "add": preset_keys,
                "list": None,
                "remove": provider_names,
                "set": provider_names,
                "use": provider_names,
                "models": provider_names,
            },
            "/use": provider_names,
            "/model": {**model_names, "list": None},
            "/models": provider_names,
            "/system": {"clear": None},
            "/tools": {"on": None, "off": None},
            "/sudo": {"on": None, "off": None},
            "/render": {"on": None, "off": None},
            "/set": {"max_tokens": None, "temperature": None},
            "/theme": {name: None for name in THEMES},
            "/scrum": {"on": None, "off": None, "show": None},
            "/clear": None,
            "/compact": None,
            "/history": None,
            "/threads": None,
            "/resume": {"latest": None},
            "/new": None,
            "/save": None,
            "/load": None,
            "/config": None,
            "/exit": None,
            "/quit": None,
        }

    hist_path = app.cfg.path.parent / "history"
    session = PromptSession(
        history=FileHistory(str(hist_path)),
        style=DynamicStyle(lambda: app._pt_style),
    )

    def bottom_toolbar():
        c = app.cfg
        p = html_escape(c.current_provider or "no-provider")
        m = html_escape(c.current_model or "no-model")
        st = app.session_tokens
        flags = [
            "<tb-flag>tools</tb-flag>" if c.settings.get("tools_enabled") else "no-tools"
        ]
        if app.auto_approve:
            flags.append("<tb-flag-warn>sudo</tb-flag-warn>")
        if not c.settings.get("render_markdown"):
            flags.append("plain")
        return HTML(
            f" <tb-model>{p} · {m}</tb-model>   "
            f"⬆{_fmt_k(st['in'])} ⬇{_fmt_k(st['out'])}   "
            f"ctx ~{_fmt_k(app.context_estimate())}   {' '.join(flags)} "
        )

    def prompt() -> str:
        return session.prompt(
            [("class:prompt", "› ")],
            completer=NestedCompleter.from_nested_dict(completer_dict()),
            bottom_toolbar=bottom_toolbar,
        )

    return prompt


def _maybe_resume(app: App, cont: bool, resume: str | None) -> None:
    """Adopt a prior thread when -c/--continue or -r/--resume is given.

    ``resume`` is None if unset, "" for a bare ``-r`` (most recent), or an
    id/prefix. ``cont`` also means "most recent".
    """
    if resume is None and not cont:
        return
    if resume:  # a specific id / prefix
        t = threads.load_thread(resume)
        if t is None:
            app.console.print(
                f"[warn]No thread matching '{resume}' in this project.[/warn] "
                "[dim]List them with /threads.[/dim]"
            )
            return
    else:  # -c, or -r with no id → the most recent thread here
        t = threads.latest_thread()
        if t is None:
            app.console.print("[dim]No previous thread in this project to resume.[/dim]")
            return
    app._adopt_thread(t)


def repl(app: App, cont: bool = False, resume: str | None = None) -> None:
    console = app.console
    clear_viewport(console)
    console.print(
        Panel.fit(
            f"[bold]{gradient('goldcomb')}[/bold] [dim]v{__version__}[/dim] "
            "— multi-provider AI agent\n"
            "[dim]/help for commands · /resume to continue a thread · /exit to quit[/dim]",
            border_style=f"dim {theme.ACCENT}",
            padding=(0, 2),
        )
    )
    prompt = _build_prompt_session(app)

    if app.auto_approve:
        console.print(
            "[warn]sudo mode — every tool call runs without confirmation. "
            "Turn off with /sudo off.[/warn]"
        )
    if app.cfg.current_provider:
        console.print(
            f"[dim]Using[/dim] [accent2]{app.cfg.current_provider}[/accent2] / "
            f"[accent]{app.cfg.current_model or '(no model set — /model <name>)'}[/accent]"
        )
    elif sys.stdin.isatty():
        console.print("[warn]No provider configured yet — let's add one.[/warn]")
        app.cmd_setup([])
    else:
        console.print(
            "[warn]No provider configured.[/warn] Run [cmd]/setup[/cmd]."
        )
    _maybe_resume(app, cont, resume)
    while True:
        try:
            line = prompt() if prompt else input("› ")
        except EOFError:
            console.print("\n[dim]bye[/dim]")
            break
        except KeyboardInterrupt:
            continue
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            try:
                cont = app.handle_command(line)
            except Exception as e:  # noqa: BLE001 - a bad command must not crash the REPL
                app.renderer.stop_all()
                console.print(f"[err]Command error:[/err] {escape(str(e))}")
                continue
            if not cont:
                console.print("[dim]bye[/dim]")
                break
            continue
        try:
            app.run_turn(line)
        except KeyboardInterrupt:
            app.renderer.stop_all()
            console.print("\n[warn]⏹ interrupted[/warn]")
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; never kill the REPL
            app.renderer.stop_all()
            console.print(f"[err]Unexpected error:[/err] {escape(str(e))}")


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    if effective_argv and effective_argv[0] == "config":
        from .config_cli import run as run_config
        return run_config(effective_argv[1:])

    parser = argparse.ArgumentParser(
        prog="goldcomb",
        description="A provider-agnostic, Claude-Code-style terminal AI agent.",
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt (non-interactive).")
    parser.add_argument("-p", "--print", dest="oneshot", metavar="PROMPT",
                        help="Answer a single prompt and exit.")
    parser.add_argument("--provider", help="Override the active provider for this run.")
    parser.add_argument("--model", help="Override the active model for this run.")
    parser.add_argument("--no-tools", action="store_true", help="Disable file/shell tools.")
    parser.add_argument("--sudo", action="store_true",
                        help="Auto-approve all tool calls without asking (like /sudo on).")
    parser.add_argument("--engine", choices=["native", "claude"], default=None,
                        help="Execution engine: 'native' (goldcomb's own tool "
                             "loop, any provider) or 'claude' (delegate the turn "
                             "to the Claude Agent SDK / Claude Code harness).")
    parser.add_argument("--serve", action="store_true",
                        help="Headless session for GUIs: NDJSON events on stdout, "
                             "commands on stdin (see goldcomb/server.py).")
    parser.add_argument("--agent-name", metavar="NAME",
                        help="Identity for this agent: stamped on scrum-ticket "
                             "assignees and thread history (default: goldcomb).")
    parser.add_argument("--role", metavar="ROLE",
                        help="Persona for this agent, e.g. 'planner' (the "
                             "Tickets-board scrum master). Adds a role block "
                             "to the system prompt; unknown roles are ignored.")
    parser.add_argument("--team", metavar="TEXT",
                        help="Team context for this agent (its lead, peers, "
                             "and reports in the project's agent tree); "
                             "appended to the system prompt.")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true",
                        help="Resume the most recent thread for this directory.")
    parser.add_argument("-r", "--resume", metavar="ID", nargs="?", const="",
                        help="Resume a thread by id/prefix (or list them if no id given).")
    parser.add_argument("-V", "--version", action="version",
                        version=f"goldcomb {__version__}")
    args = parser.parse_args(argv)

    cfg = Config.load()
    if args.agent_name:
        scrum_mod.set_agent(args.agent_name)
        threads.set_agent_name(args.agent_name)
    if args.role:
        from .roles import role_prompt as _rp
        if _rp(args.role) is None:
            print(f"Unknown role {args.role!r} — continuing without one.",
                  file=sys.stderr)
        else:
            # In-memory only: role is per-session, never written to config.
            cfg.settings["role"] = args.role
    if args.team:
        cfg.settings["team"] = args.team  # in-memory only, like role

    if args.engine:
        # In-memory only: an explicit --engine overrides for this run without
        # rewriting the persisted default (parallel serve sessions share config).
        cfg.settings["engine"] = args.engine

    if args.serve:
        from .server import serve

        if args.no_tools:
            cfg.settings["tools_enabled"] = False
        if args.provider or args.model:
            # In-memory only: parallel GUI sessions share the config file and
            # must not rewrite its default provider/model.
            provider = args.provider or cfg.current_provider
            if provider not in cfg.providers:
                print(f"Unknown provider: {provider}", file=sys.stderr)
                return 2
            cfg.current = {
                "provider": provider,
                "model": args.model or cfg.current.get("model", ""),
            }
        return serve(cfg, sudo=args.sudo)

    if args.provider:
        if args.provider in cfg.providers:
            cfg.use_provider(args.provider, args.model)
        else:
            print(f"Unknown provider: {args.provider}", file=sys.stderr)
            return 2
    elif args.model:
        try:
            cfg.set_model(args.model)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if args.no_tools:
        cfg.settings["tools_enabled"] = False

    console = make_console()
    app = App(cfg, console)
    if args.sudo:
        app.auto_approve = True

    # Determine one-shot prompt: -p, positional args, or piped stdin.
    oneshot = args.oneshot
    if not oneshot and args.prompt:
        oneshot = " ".join(args.prompt)
    if not oneshot and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            oneshot = piped

    if oneshot:
        if not cfg.current_provider:
            print("No provider configured. Run `goldcomb` and use /setup.", file=sys.stderr)
            return 2
        app.auto_approve = True  # non-interactive: don't block on confirmations
        # Only persist a one-shot to a thread when explicitly continuing/resuming,
        # so a quick `goldcomb -p "..."` leaves nothing behind.
        app.persist = bool(args.cont or args.resume is not None)
        _maybe_resume(app, args.cont, args.resume)
        try:
            app.run_turn(oneshot)
        except KeyboardInterrupt:
            return 130
        return 0

    try:
        repl(app, cont=args.cont, resume=args.resume)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
