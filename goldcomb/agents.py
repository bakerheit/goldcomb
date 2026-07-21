"""Sub-agents: autonomous workers the lead agent can deploy via deploy_agent.

A sub-agent is a fresh, headless agentic loop: its own message history, its
own system prompt, and the same file/shell tools — minus deploy_agent itself,
so agents can't recurse. It runs to completion without user interaction and
its final message is returned to the lead agent as the tool result.

The deploying model may pick any configured provider and any model for the
worker (``resolve_target``); by default it inherits whatever the session is
currently using.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from . import scrum
from . import threads
from .config import DEFAULT_MODEL_BY_TYPE, Config
from .providers import Completed, Message, Provider, TextDelta
from .tools import Tool, available_tools, describe_call, missing_required_args

#: Progress callback: kind is "tool" (a tool call summary) or "status"
#: (a short phase description).
OnEvent = Callable[[str, str], None]

MAX_SUBAGENT_ITERATIONS = 15

#: Where handle records persist for out-of-process readers (the macOS app).
REGISTRY_DIR = Path(".ai") / "agents"

#: Per-agent deploy config the macOS app writes from its Agents tab: an agent's
#: user-chosen default model. The deploy flow honors it so a lead deploying a
#: pre-configured agent runs it on the model the user picked (NEXA "both").
AGENT_CONFIG_FILE = REGISTRY_DIR / "agent-config.json"


def configured_default(name: str) -> tuple[str | None, str | None]:
    """The user-configured default (provider, model) for the agent called
    ``name``, from ``.ai/agents/agent-config.json`` — written by the macOS
    app's Agents tab. ``(None, None)`` when unconfigured or the file is
    absent/unreadable. Matches the full human name and, as a fallback, the bare
    functional label inside ``"Name (label)"`` so a deploy that passed the raw
    label still resolves."""
    try:
        data = json.loads(AGENT_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return (None, None)
    entries = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return (None, None)
    entry = entries.get(name)
    if entry is None:
        m = re.search(r"\(([^)]+)\)\s*$", name)  # "Quill Ashwood (swift-worker-2)"
        if m:
            entry = entries.get(m.group(1))
    if not isinstance(entry, dict):
        return (None, None)
    return (entry.get("provider") or None, entry.get("model") or None)

#: Heartbeat: a running worker touches the scrum board on its own behalf this
#: often, so a long tool-less stretch (a slow model turn) doesn't look dead.
_HEARTBEAT_INTERVAL_S = 60.0

SUBAGENT_INSTRUCTIONS = """\
You are a goldcomb sub-agent, deployed by a lead agent to complete one task. The \
working directory is {cwd}. Today's date is {date}. You have tools to read, \
write, and edit files and run shell commands. Work autonomously: you cannot \
ask questions, and you cannot see the lead agent's conversation — everything \
you know is in the task brief.

Verify your work: after writing or changing code, run it or its tests. Read a \
file before you edit it; if the same command fails twice, change your approach \
instead of repeating it. For repo state, prefer the git_status/git_diff/\
git_log/git_branch tools over run_bash("git ...").

Your FINAL message is your report back to the lead agent — it is the only \
thing returned, so make it self-contained: what you did, what you found, exact \
paths/commands/values the lead agent needs, and anything left broken or \
unfinished. No pleasantries."""


#: How a sub-agent run ended. "completed" includes hitting the step limit
#: (the worker still filed a report — a forced wrap-up counts as completing).
#: The empty-report cases keep their own reasons so a silent worker is
#: distinguishable from a talkative one.
STOP_REASONS = ("completed", "error", "step_limit", "context_exhausted")

#: Suggested UI colors per state, for the macOS sidebar (NEXA-14).
_STATE_COLORS = {
    "starting": "gray",
    "running": "blue",
    "completed": "green",
    "step_limit": "yellow",
    "context_exhausted": "yellow",
    "error": "red",
}


@dataclass
class SubAgentResult:
    report: str
    usage: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0})
    iterations: int = 0
    tool_calls: int = 0
    #: One of STOP_REASONS. "completed" means the worker finished and filed a
    #: report (forced wrap-ups after the step ceiling count). "error" /
    #: "step_limit" / "context_exhausted" mean no report was produced and the
    #: report field holds the diagnostic footer instead.
    stop_reason: str = "completed"
    #: Model round-trips actually consumed (mirrors iterations under a stable
    #: name for the diagnostic footer).
    steps_used: int = 0
    #: The last assistant message, whatever it contained — the deploy tool
    #: falls back to it when no final report was produced.
    last_assistant_text: str = ""
    #: Where the transcript was autosaved (.ai/threads/<id>.jsonl), if at all.
    transcript_path: str | None = None
    error: str | None = None

    def diagnostic_footer(self) -> str:
        """The "never return empty" guarantee: stop reason, steps, tool calls,
        and where the transcript lives — everything needed to inspect the run.
        """
        lines = [
            "---",
            f"stop_reason: {self.stop_reason}",
            f"steps_used: {self.steps_used}  tool_calls: {self.tool_calls}",
        ]
        if self.error:
            lines.append(f"error: {self.error}")
        if self.transcript_path:
            lines.append(f"transcript: {self.transcript_path}")
        lines.append(
            "The sub-agent produced no final report; its last assistant "
            "message (if any) is shown above."
        )
        return "\n".join(lines)


def resolve_target(
    cfg: Config, provider_name: str | None, model: str | None
) -> tuple[str, str]:
    """Resolve the (provider, model) a sub-agent should run on.

    Falls back to the session's current provider/model when unspecified; a
    provider given without a model falls back to that provider type's default.
    Raises ValueError with a model-correctable message on bad input.
    """
    name = provider_name or cfg.current_provider
    if not name:
        raise ValueError("No provider configured.")
    if name not in cfg.providers:
        raise ValueError(
            f"Unknown provider '{name}'. Configured: {', '.join(cfg.providers) or '(none)'}"
        )
    if model:
        resolved = model
    elif name == cfg.current_provider and cfg.current_model:
        resolved = cfg.current_model
    else:
        ptype = cfg.providers[name].get("type", "")
        cached = cfg.models_for(name)
        resolved = cached[0] if cached else DEFAULT_MODEL_BY_TYPE.get(ptype, "")
    if not resolved:
        raise ValueError(
            f"No default model known for provider '{name}' — pass a model explicitly."
        )
    return name, resolved


#: Tools a sub-agent never gets: no recursion, and no talking to the user —
#: a sub-agent's contract is full autonomy on a standalone brief.
_LEAD_ONLY_TOOLS = ("deploy_agent", "ask_user")


def subagent_tools() -> list[Tool]:
    """The toolset a sub-agent gets: everything currently available (which
    honors per-project scrum opt-in) except lead-only tools."""
    return [t for t in available_tools() if t.name not in _LEAD_ONLY_TOOLS]


def subagent_system_prompt(label: str | None = None) -> str:
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = Path("(working directory unavailable)")
    base = SUBAGENT_INSTRUCTIONS.format(cwd=cwd, date=date.today().isoformat())
    # A repeatedly-deployed worker keeps its lessons: its per-label memory
    # file rides along on every deploy under that label.
    if label:
        from . import memory as memory_mod
        own = memory_mod.read_memory(label)
        if own:
            base += (
                f"\n\nYour memory (private to '{label}', kept across deploys — "
                "maintain it with the memory tool):\n" + own
            )
    return base


# ---- the registry ------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
#: Currently live handles, newest last. Terminal handles persist on disk
#: (``.ai/agents/<id>.json``) but are dropped from this in-memory map.
REGISTRY: dict[str, "SubAgentHandle"] = {}


def registry_snapshot() -> dict:
    """JSON-serializable view of every sub-agent this process knows about.

    Shape (consumed by the macOS app, NEXA-14)::

        {"version": 1, "generated_at": <epoch float>,
         "agents": [ <handle.status()> plus {"pid": int}, ... ]}

    Each agent entry: id, label, state ("starting" | "running" | a terminal
    STOP_REASON), color (suggested UI color for the state), started_at,
    last_event_at, ended_at, exited_at, n_tool_calls, report_saved (bool),
    error, transcript_path, record_path (the on-disk copy of this entry).
    """
    with _REGISTRY_LOCK:
        agents_list = [dict(h.status(), pid=os.getpid()) for h in REGISTRY.values()]
    return {
        "version": 1,
        "generated_at": time.time(),
        "agents": agents_list,
    }


class SubAgentHandle:
    """A live (or finished) sub-agent run: non-blocking launch, poll, wait.

    ``launch()`` starts the worker on a daemon thread and returns
    immediately; ``status()`` is cheap and thread-safe; ``wait(timeout)``
    blocks for the result. ``last_event_at`` is bumped inside the tool loop
    on every tool call and every model round-trip — that timestamp is the
    liveness signal. State transitions: starting -> running -> a terminal
    STOP_REASON ("completed" | "error" | "step_limit" | "context_exhausted").
    """

    def __init__(
        self,
        provider: Provider,
        model: str,
        task: str,
        *,
        label: str = "agent",
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        max_iterations: int = MAX_SUBAGENT_ITERATIONS,
        on_event: OnEvent | None = None,
        heartbeat: bool = True,
        ticket: str | None = None,
    ):
        self.id = f"{label}-{threads.new_thread_id()}"
        self.label = label
        self.provider = provider
        self.model = model
        self.task = task
        self.system = system
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_iterations = max_iterations
        self.on_event = on_event
        #: Touch the scrum board as this worker while it runs (see _beat).
        self.heartbeat = heartbeat
        #: Ticket to comment the outcome on at exit (the spawner does it when
        #: the worker can't). None = nothing to comment on.
        self.ticket = ticket

        self._lock = threading.Lock()
        self._state = "starting"
        self._started_at = time.time()
        self._last_event_at = self._started_at
        self._ended_at: float | None = None
        self._n_tool_calls = 0
        self._thread: threading.Thread | None = None
        self.result: SubAgentResult | None = None

    # -- lifecycle ---------------------------------------------------------

    def launch(self) -> "SubAgentHandle":
        """Start the run on a background thread; return immediately."""
        self._thread = threading.Thread(
            target=self._run, name=f"subagent-{self.label}", daemon=True
        )
        with _REGISTRY_LOCK:
            REGISTRY[self.id] = self
        # Persist the "starting" record BEFORE the thread runs: a fast worker
        # can finish (and persist its terminal state) in less time than this
        # write takes, and the stale "starting" doc must not land on top.
        self._persist_record()
        self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> SubAgentResult | None:
        """Block until the run ends (or timeout); the result, or None if still
        running. A provider failure is captured in the result (stop_reason
        "error") rather than raised."""
        if self._thread is not None:
            self._thread.join(timeout)
        return self.result

    def poll(self) -> SubAgentResult | None:
        """The result if the run has ended, else None. Never blocks."""
        return self.result

    def status(self) -> dict:
        """A JSON-safe liveness snapshot; see registry_snapshot for the shape."""
        with self._lock:
            return {
                "id": self.id,
                "label": self.label,
                "state": self._state,
                "color": _STATE_COLORS.get(self._state, "gray"),
                "started_at": self._started_at,
                "last_event_at": self._last_event_at,
                "ended_at": self._ended_at,
                "exited_at": self._ended_at,
                "n_tool_calls": self._n_tool_calls,
                "report_saved": bool(self.result and self.result.report),
                "error": self.result.error if self.result else None,
                "transcript_path": self.result.transcript_path if self.result else None,
                "record_path": str(self._record_path()),
            }

    # -- internals ---------------------------------------------------------

    def _touch(self) -> None:
        """A tool call happened: bump the liveness timestamp and the counter."""
        with self._lock:
            self._last_event_at = time.time()
            self._n_tool_calls += 1

    def _beat(self) -> None:
        """Heartbeat: bump liveness and touch the scrum board as this worker,
        so ``scrum.stale_agents`` sees a running deploy as alive even between
        board mutations. Fires at most once per _HEARTBEAT_INTERVAL_S."""
        with self._lock:
            self._last_event_at = time.time()
        now = time.monotonic()
        if self.heartbeat and now - getattr(self, "_last_beat", 0) >= _HEARTBEAT_INTERVAL_S:
            self._last_beat = now
            try:
                scrum.heartbeat(self.label)
            except Exception:  # noqa: BLE001 - liveness must never kill a run
                pass

    def _record_path(self) -> Path:
        return REGISTRY_DIR / f"{self.id}.json"

    def _persist_record(self) -> None:
        """Best-effort on-disk copy of status(), for out-of-process readers."""
        try:
            rec = dict(self.status(), pid=os.getpid())
            rec.pop("record_path", None)
            REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
            path = self._record_path()
            # Unique tmp per writer: the launch-thread persist and the run
            # thread's end-of-run persist can overlap; sharing one tmp file
            # let a rename land mid-write (a record with two concatenated
            # JSON docs). Each writer renames its own complete document.
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(rec, indent=2) + "\n")
            tmp.replace(path)
        except OSError:
            pass

    def _run(self) -> None:
        with self._lock:
            self._state = "running"
            self._last_event_at = time.time()
        try:
            result = run_subagent(
                self.provider,
                self.model,
                self.task,
                system=self.system,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                max_iterations=self.max_iterations,
                on_event=self.on_event,
                label=self.label,
                handle=self,
            )
        except Exception as e:  # noqa: BLE001 - a dead worker still gets a record
            result = SubAgentResult(report="", stop_reason="error", error=str(e))
        with self._lock:
            self._state = result.stop_reason
            self._ended_at = time.time()
            self._last_event_at = self._ended_at
        self.result = result
        self._persist_record()
        self._comment_outcome(result)
        with _REGISTRY_LOCK:
            REGISTRY.pop(self.id, None)

    def _comment_outcome(self, result: SubAgentResult) -> None:
        """On exit, leave the outcome on the worker's ticket. A no-report exit
        is marked *suspect* in the comment — no automatic status change."""
        if not self.ticket:
            return
        when = time.strftime("%H:%M:%S")
        if result.report:
            text = (
                f"[{when}] sub-agent '{self.label}' exited "
                f"({result.stop_reason}, {result.steps_used} step(s), "
                f"{result.tool_calls} tool call(s)). Outcome: "
                f"{result.report[:500]}"
            )
        else:
            text = (
                f"[{when}] SUSPECT: sub-agent '{self.label}' exited "
                f"({result.stop_reason}) with NO final report after "
                f"{result.steps_used} step(s), {result.tool_calls} tool "
                f"call(s). Verify its work before trusting this ticket's state."
            )
            if result.transcript_path:
                text += f" Transcript: {result.transcript_path}"
        try:
            scrum.scrum({"action": "comment", "ticket": self.ticket, "text": text})
        except Exception:  # noqa: BLE001 - the run's record must not depend on the board
            pass


def launch_subagent(provider: Provider, model: str, task: str, **kwargs) -> SubAgentHandle:
    """Create a handle and start the run in the background."""
    return SubAgentHandle(provider, model, task, **kwargs).launch()


def run_subagent(
    provider: Provider,
    model: str,
    task: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    max_iterations: int = MAX_SUBAGENT_ITERATIONS,
    on_event: OnEvent | None = None,
    label: str = "agent",
    handle: SubAgentHandle | None = None,
) -> SubAgentResult:
    """Run one sub-agent to completion and return its final report.

    Thin blocking wrapper around the tool loop; ``launch_subagent`` runs this
    on a background thread. Headless: streaming events are consumed silently
    (progress surfaces only through ``on_event``), and dangerous tools run
    without confirmation — the deployment itself is what the user approved.
    A provider failure on the final wrap-up is captured as stop_reason
    "error"; KeyboardInterrupt aborts the whole turn as usual. Every run
    autosaves its transcript to .ai/threads/ as it goes, so the run is
    inspectable live or dead.
    """
    emit = on_event or (lambda kind, text: None)
    tools = subagent_tools()
    tools_by_name = {t.name: t for t in tools}
    specs = [t.spec for t in tools]
    system = system or subagent_system_prompt(label)
    messages: list[Message] = [Message(role="user", content=task)]
    result = SubAgentResult(report="")
    recorder = _TranscriptRecorder(label, model=model)

    def event() -> None:
        """One model round-trip happened: liveness + transcript checkpoint."""
        recorder.save(messages)
        if handle is not None:
            handle._beat()

    for iteration in range(max_iterations):
        result.iterations = iteration + 1
        result.steps_used = iteration + 1
        emit("status", "thinking")
        message, stop = _stream_headless(
            provider, messages, model, system, specs, max_tokens, temperature, result
        )
        messages.append(message)
        if message.content:
            result.last_assistant_text = message.content
        event()
        if stop != "tool_use" or not message.tool_calls:
            if message.content:
                # A clean finish; the reason is already "completed".
                result.report = message.content
            else:
                # The "silent worker" case: the model produced no report and
                # no tool calls. Treat it like context exhaustion so the
                # caller can tell it apart from a real completion.
                result.stop_reason = "context_exhausted"
                result.transcript_path = recorder.saved_path()
                result.report = result.diagnostic_footer()
            result.transcript_path = recorder.saved_path()
            return result
        for call in message.tool_calls:
            messages.append(_run_tool(tools_by_name, call, emit, result, handle))

    # Hit the iteration ceiling — force a final wrap-up without tools.
    emit("status", "summarizing")
    messages.append(
        Message(
            role="user",
            content="You've reached the tool-call limit. In 2-4 sentences, report "
            "what you completed, what you found, and what is still unfinished.",
        )
    )
    try:
        message, _ = _stream_headless(
            provider, messages, model, system, None, max_tokens, temperature, result
        )
    except Exception as e:  # noqa: BLE001 - record the failure, don't raise
        result.stop_reason = "error"
        result.error = str(e)
        # Set the transcript path BEFORE building the footer so the record
        # pointer is part of the report (NEXA-17: always inspectable).
        result.transcript_path = recorder.saved_path()
        result.report = result.diagnostic_footer()
        return result
    messages.append(message)
    if message.content:
        result.last_assistant_text = message.content
    if message.content:
        # The wrap-up produced a report — the run completed despite the ceiling.
        result.report = message.content
    else:
        result.stop_reason = "step_limit"
        # Transcript path first: the footer must name where the record lives.
        result.transcript_path = recorder.saved_path()
        result.report = result.diagnostic_footer()
    recorder.save(messages)
    result.transcript_path = recorder.saved_path()
    return result


def _stream_headless(
    provider: Provider,
    messages: list[Message],
    model: str,
    system: str,
    specs,
    max_tokens: int,
    temperature: float | None,
    result: SubAgentResult,
) -> tuple[Message, str]:
    completed: Completed | None = None
    text_parts: list[str] = []
    for ev in provider.stream(
        messages,
        model=model,
        system=system,
        tools=specs,
        max_tokens=max_tokens,
        temperature=temperature,
    ):
        if isinstance(ev, Completed):
            completed = ev
        elif isinstance(ev, TextDelta):
            text_parts.append(ev.text)
    if completed is None:
        return Message(role="assistant", content="".join(text_parts)), "end_turn"
    usage = completed.usage or {}
    result.usage["in"] += usage.get("input_tokens", 0)
    result.usage["out"] += usage.get("output_tokens", 0)
    return completed.message, completed.stop_reason


def _run_tool(
    tools_by_name: dict[str, Tool],
    call,
    emit: OnEvent,
    result: SubAgentResult,
    handle: SubAgentHandle | None = None,
) -> Message:
    def reply(content: str) -> Message:
        return Message(role="tool", content=content, tool_call_id=call.id, name=call.name)

    tool = tools_by_name.get(call.name)
    if tool is None:
        return reply(f"Error: unknown tool '{call.name}'")
    missing = missing_required_args(tool, call.arguments)
    if missing:
        return reply(
            f"Error: missing required argument(s): {', '.join(missing)}. "
            "Provide them and retry."
        )
    result.tool_calls += 1
    if handle is not None:
        handle._touch()
    emit("tool", describe_call(call.name, call.arguments))
    try:
        return reply(tool.run(call.arguments))
    except Exception as e:  # noqa: BLE001 - surface tool errors to the model
        return reply(f"Error executing tool: {e}")


class _TranscriptRecorder:
    """Autosaves the sub-agent's transcript to .ai/threads/ on every event.

    Uses threads.py's vendor-neutral JSONL export, with the worker's label as
    the header ``agent`` field — so anyone can open the file and see exactly
    which worker produced it, mid-run or after death. The thread title is the
    task's first line; the id stays stable across saves (one file per run).
    """

    def __init__(self, label: str, model: str | None = None):
        self.label = label
        self.model = model
        self.thread: threads.Thread | None = None

    def save(self, messages: list[Message]) -> None:
        try:
            if self.thread is None:
                self.thread = threads.new_thread(model=self.model)
            self.thread.title = _task_title(messages)
            self.thread.messages = [
                dict(m.to_dict(), timestamp=threads._now()) for m in messages
            ]
            prev = threads.AGENT_NAME
            threads.set_agent_name(f"goldcomb-subagent:{self.label}")
            try:
                threads.save_thread(self.thread)
            finally:
                threads.set_agent_name(prev)
        except Exception:  # noqa: BLE001 - autosave is best-effort
            pass

    def saved_path(self) -> str | None:
        if self.thread is None:
            return None
        try:
            return str(threads.ai_threads_dir() / f"{self.thread.id}.jsonl")
        except Exception:  # noqa: BLE001
            return None


def _task_title(messages: list[Message]) -> str:
    for m in messages:
        if m.role == "user" and (m.content or "").strip():
            text = " ".join(m.content.split())
            return text[:60] + ("…" if len(text) > 60 else "")
    return "(sub-agent run)"
