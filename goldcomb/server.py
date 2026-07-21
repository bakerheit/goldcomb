"""Headless serve mode: drive a goldcomb agent over structured stdio.

``goldcomb --serve`` turns the process into one agent session speaking NDJSON:
every line on stdout is one JSON event, every line on stdin is one JSON
command. GUIs (like the macOS app) spawn one process per agent session, so
multiple agents run in isolated processes; sub-agents deployed inside a
session surface through the same event stream.

Events (stdout, one JSON object per line):
  ready            providers/current/version handshake, sent once at startup
  status           {"label": str | null} — spinner text; null clears it
  message_start    assistant response begins {"provider", "model"}
  delta            {"text"} — streamed response fragment
  message_end      {"text"} — the complete response
  tool_call        {"summary"} — a tool is about to run
  tool_result      {"output"} — what it returned
  nudge            {"text"} — loop-guard warnings
  usage            {"in","out","cached","session_in","session_out",
                   "session_cached"} — "cached" is prompt-cache reads, billed
                   at ~10% of "in" and not included in it
  confirm_request  {"summary"} — reply with a confirm command
  ask_request      {"questions":[{question,header?,options?,multi_select?}]}
                   — the model is asking the user; reply with an answer command
  using            {"provider","model"} — after a use command
  subagent_start   {"id","label","task","parent","provider","model"} — a
                   deploy_agent worker began; task is truncated, parent is the
                   deploying session's thread id (null before the first save)
  subagent_end     {"id","label","stop_reason","iterations","tool_calls",
                   "usage":{"in","out"},"transcript_path","error"} — it finished
  scrum            {"enabled","message"} — after a scrum command
  scrum_result     {"action","message","ok"} — reply to a scrum_action command
  thread           {"thread_id"} — the conversation's saved-thread id
                                    (emitted at turn start, once known)
  turn_end         {"session_in","session_out","thread_id"} — the turn finished
  threads          {"threads":[{id,title,updated,cwd,provider,model,
                   message_count}]} — reply to a threads command
  git_status       {"branch","ahead","behind","files":[{path,status}]} —
                   reply to a git_status command (working-tree state)
  git_diff         {"path","staged","diff","truncated"} — reply to a git_diff
                   command: one file's unified diff (diff carries a
                   "new file, no diff" notice for untracked files; truncated
                   is true when the diff hit the size cap)
  resumed          {"thread_id","title","message_count"} — reply to resume
  compacted        {"ok","before","after","reason"} — reply to a compact
                   command; history was summarized (before→after messages),
                   or ok=false with a reason ("too-short"/"empty-summary")
  models           {"provider","models":[...],"ok","error"?} — reply to a
                   models command: the provider's live catalog (ok=true), or
                   the built-in fallback list with ok=false + error on a fetch
                   failure
  interrupted      the turn was aborted (e.g. SIGINT)
  error            {"message"}

Commands (stdin, one JSON object per line):
  {"type":"user","text":...}                       run one agentic turn
  {"type":"threads"}                               list saved threads
  {"type":"git_status"}                            working-tree status (polled
                                                   mid-turn or idle)
  {"type":"git_diff","path":<repo-relative>,       one file's unified diff
   "staged":true|false}                            (staged = the index; default
                                                   false = the working tree)
  {"type":"resume","id":<thread-id|prefix>}        resume a saved thread
  {"type":"confirm","decision":"yes|no|always|abort"}   answer confirm_request
  {"type":"answer","answers":["...", ...]}         answer ask_request, one
                                                   string per question in order
                                                   ("" = no answer)
  {"type":"use","provider":...,"model":...}        switch (in-memory only —
                                                   never saved, so parallel
                                                   sessions don't fight over
                                                   the config file)
  {"type":"clear"}                                 start a fresh conversation
                                                   (drops history, new thread)
  {"type":"compact"}                               summarize history in place
                                                   (keeps the thread; replies
                                                   with a compacted event)
  {"type":"models","provider":<name>}              live-fetch a provider's full
                                                   model catalog (defaults to
                                                   current); replies with models
  {"type":"sudo","on":true|false}                  toggle auto-approve
  {"type":"scrum","on":true|false}                 enable/disable per-project
                                                   ticket tracking (the scrum
                                                   tool is only offered to the
                                                   model while enabled)
  {"type":"scrum_action","action":"task_add",...}  run one scrum-board action
                                                   (any field scrum() takes,
                                                   e.g. story/title/status) —
                                                   how a GUI edits the board
  {"type":"exit"}                                  shut down

Anything the CLI would print for a human (errors, notices) goes to stderr as
plain text — a GUI can show it as a log. stdout carries only NDJSON.

Thread history is persisted by the CLI layer the same way interactive sessions
are: every turn autosaves to the project's thread store, and each save is
exported in a vendor-neutral interchange format at ``<cwd>/.ai/threads/``
(one ``<thread-id>.jsonl`` per conversation — see goldcomb.threads), so any AI
tool or GUI can read history straight from the project folder, and threads
other tools write there become resumable here.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_MODEL_BY_TYPE, Config

Emit = Callable[[dict[str, Any]], None]


def _models_for(cfg: Config, name: str) -> list[str]:
    """Models to offer for a provider: the live-fetched cache if we have one,
    else the adapter's built-in list. Without the fallback a freshly-added
    provider surfaces no models until the user runs a live fetch, so a GUI's
    model picker shows only "default model" (the bug this fixes). Mirrors the
    CLI's ``cached or default_models_for(ptype)``."""
    from .providers import default_models_for

    cached = cfg.models_for(name)
    if cached:
        return cached
    ptype = cfg.providers.get(name, {}).get("type", "")
    return default_models_for(ptype)


class JsonEventRenderer:
    """Drop-in for ui.Renderer that emits NDJSON events instead of drawing.

    Implements the exact surface App drives: status, message streaming, tool
    display, usage, teardown, and the resize hook (a no-op here).
    """

    def __init__(self, emit: Emit):
        self._emit = emit
        self.markdown = True
        self.fancy = False
        self.footer = None  # assigned by App; meaningless without a terminal
        self._buf: list[str] = []
        self._last_status: str | None = None

    # ---- status ------------------------------------------------------------

    def start_status(self, label: str) -> None:
        self.update_status(label)

    def update_status(self, label: str) -> None:
        if label != self._last_status:
            self._last_status = label
            self._emit({"event": "status", "label": label})

    def stop_status(self) -> None:
        if self._last_status is not None:
            self._last_status = None
            self._emit({"event": "status", "label": None})

    # ---- assistant message -------------------------------------------------

    def begin_message(self, provider: str, model: str) -> None:
        self.stop_status()
        self._buf = []
        self._emit({"event": "message_start", "provider": provider, "model": model})

    def message_delta(self, text: str) -> None:
        self._buf.append(text)
        self._emit({"event": "delta", "text": text})

    def end_message(self) -> None:
        self._emit({"event": "message_end", "text": "".join(self._buf)})
        self._buf = []

    # ---- tools -------------------------------------------------------------

    def tool_call(self, summary: str) -> None:
        self._emit({"event": "tool_call", "summary": summary})

    def tool_result(self, output: str) -> None:
        self._emit({"event": "tool_result", "output": output})

    def nudge(self, msg: str) -> None:
        self._emit({"event": "nudge", "text": msg})

    # ---- sub-agents ----------------------------------------------------------

    def subagent_start(
        self,
        *,
        id: str,
        label: str,
        task: str,
        parent: str | None,
        provider: str,
        model: str,
    ) -> None:
        self._emit(
            {
                "event": "subagent_start",
                "id": id,
                "label": label,
                "task": task,
                "parent": parent,
                "provider": provider,
                "model": model,
            }
        )

    def subagent_end(
        self,
        *,
        id: str,
        label: str,
        stop_reason: str,
        iterations: int,
        tool_calls: int,
        usage: dict[str, int],
        transcript_path: str | None,
        error: str | None,
    ) -> None:
        self._emit(
            {
                "event": "subagent_end",
                "id": id,
                "label": label,
                "stop_reason": stop_reason,
                "iterations": iterations,
                "tool_calls": tool_calls,
                "usage": usage,
                "transcript_path": transcript_path,
                "error": error,
            }
        )

    # ---- misc --------------------------------------------------------------

    def usage(self, usage: dict, session: dict) -> None:
        self._emit(
            {
                "event": "usage",
                "in": usage.get("input_tokens", 0),
                "out": usage.get("output_tokens", 0),
                # Prompt-cache reads: billed at ~10% of "in", and excluded from
                # it. Zero across a long session means caching isn't landing.
                "cached": usage.get("cache_read_input_tokens", 0),
                "session_in": session.get("in", 0),
                "session_out": session.get("out", 0),
                "session_cached": session.get("cached", 0),
            }
        )

    def install_resize_handler(self) -> None:  # no terminal to resize
        pass

    def on_resize(self) -> None:
        pass

    def stop_all(self) -> None:
        self.stop_status()


def make_serve_app(cfg: Config, console, emit: Emit, commands) -> "object":
    """Build an App whose user-interaction points ride the NDJSON protocol.

    Defined via a factory (not at module level) so importing goldcomb.server
    stays cheap and cycle-free for tests that only need the renderer.
    """
    from .cli import App

    class ServeApp(App):
        def _confirm(self, summary: str) -> str:
            emit({"event": "confirm_request", "summary": summary})
            while True:
                cmd = commands.get()
                if cmd is None:
                    return "abort"
                if cmd.get("type") == "confirm":
                    decision = cmd.get("decision", "abort")
                    return decision if decision in ("yes", "no", "always", "abort") else "abort"
                # Any other command mid-confirmation is a protocol error; the
                # sender sees it and the question stays open.
                emit({"event": "error", "message": "expected a confirm command"})

        def _ask_user_impl(self, args: dict) -> str:
            from .cli import _valid_questions

            questions = _valid_questions(args)
            if not questions:
                return "Error: provide a non-empty 'questions' array."
            emit({"event": "ask_request", "questions": questions})
            while True:
                cmd = commands.get()
                if cmd is None:
                    return "Error: the user disconnected before answering."
                if cmd.get("type") == "answer":
                    answers = cmd.get("answers") or []
                    lines = []
                    for i, q in enumerate(questions):
                        given = str(answers[i]).strip() if i < len(answers) else ""
                        lines.append(
                            f"Q: {q['question']}\n"
                            f"A: {given or '(no answer — decide yourself)'}"
                        )
                    return "\n".join(lines)
                emit({"event": "error", "message": "expected an answer command"})

    return ServeApp(cfg, console)


def serve(cfg: Config, *, sudo: bool = False) -> int:
    """Run one agent session over stdio. Returns a process exit code."""
    from .theme import make_console

    write_lock = threading.Lock()

    def emit(obj: dict[str, Any]) -> None:
        with write_lock:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    commands: "queue.Queue[dict[str, Any] | None]" = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                emit({"event": "error", "message": f"unparseable command: {line[:200]}"})
                continue
            commands.put(cmd)
        commands.put(None)  # EOF → shut down

    # Human-facing prints (errors, notices) land on stderr as plain text.
    console = make_console(file=sys.stderr, force_terminal=False)

    app = make_serve_app(cfg, console, emit, commands)
    app.renderer = JsonEventRenderer(emit)
    app.renderer.footer = app._footer_info
    app.auto_approve = sudo

    threading.Thread(target=read_stdin, daemon=True).start()
    emit(
        {
            "event": "ready",
            "version": _version(),
            "cwd": str(Path.cwd()),
            "provider": cfg.current_provider,
            "model": cfg.current_model,
            "config_revision": cfg.config_revision,
            "providers": {
                name: {
                    "type": entry.get("type", ""),
                    "has_key": cfg.resolve_api_key(name) is not None,
                    "models": _models_for(cfg, name),
                }
                for name, entry in cfg.providers.items()
            },
        }
    )

    while True:
        try:
            cmd = commands.get()
        except KeyboardInterrupt:
            emit({"event": "interrupted"})
            continue
        if cmd is None or cmd.get("type") == "exit":
            return 0
        try:
            _dispatch(app, cfg, cmd, emit)
        except KeyboardInterrupt:
            # SIGINT mid-turn: the turn is already unwound by App's own
            # handlers; tell the client and keep serving.
            emit({"event": "interrupted"})
        except Exception as e:  # noqa: BLE001 - a bad command must not kill the session
            emit({"event": "error", "message": str(e)})


def _dispatch(app, cfg: Config, cmd: dict[str, Any], emit: Emit) -> None:
    kind = cmd.get("type")
    if kind == "user":
        text = str(cmd.get("text", "")).strip()
        if not text:
            emit({"event": "error", "message": "user command with empty text"})
            return
        if getattr(app, "thread", None) is None and getattr(app, "persist", True):
            from . import threads as threads_mod
            app.thread = threads_mod.new_thread(
                provider=cfg.current_provider, model=cfg.current_model
            )
        tid = getattr(getattr(app, "thread", None), "id", None)
        if tid:
            emit({"event": "thread", "thread_id": tid})
        try:
            app.run_turn(text)
        finally:
            emit(
                {
                    "event": "turn_end",
                    "session_in": app.session_tokens["in"],
                    "session_out": app.session_tokens["out"],
                    # The saved thread's id (once the turn has persisted one):
                    # the GUI surfaces it as the copyable "chat id".
                    "thread_id": getattr(getattr(app, "thread", None), "id", None),
                }
            )
    elif kind == "use":
        provider = cmd.get("provider") or cfg.current_provider
        if provider not in cfg.providers:
            emit({"event": "error", "message": f"unknown provider: {provider}"})
            return
        if cmd.get("model"):
            model = cmd["model"]
        elif provider == cfg.current_provider:
            model = cfg.current.get("model", "")
        else:
            ptype = cfg.providers[provider].get("type", "")
            cached = cfg.models_for(provider)
            model = cached[0] if cached else DEFAULT_MODEL_BY_TYPE.get(ptype, "")
        # In-memory only — never cfg.save(): parallel sessions share the
        # config file and must not overwrite each other's defaults.
        cfg.current = {"provider": provider, "model": model}
        emit({"event": "using", "provider": provider, "model": cfg.current_model})
    elif kind == "sudo":
        app.auto_approve = bool(cmd.get("on", True))
        emit({"event": "sudo", "on": app.auto_approve})
    elif kind == "clear":
        # Start a fresh conversation: drop the in-memory turn history and
        # detach from the saved thread so the next turn opens a new one
        # (mirrors /clear in the CLI, minus the terminal viewport reset).
        app.messages.clear()
        app.thread = None
        emit({"event": "cleared"})
    elif kind == "compact":
        # Summarize the conversation and continue from the summary (mirrors
        # /compact in the CLI). Unlike clear, the thread is kept — the next
        # autosave writes the compacted history over it.
        from .providers import ProviderError

        try:
            provider = app.get_provider()
            result = app.compact_conversation(provider)
        except ProviderError as e:
            emit({"event": "error", "message": f"compaction failed: {e}"})
            return
        if result.get("ok"):
            app._autosave()
        emit({
            "event": "compacted",
            "ok": bool(result.get("ok")),
            "before": result.get("before", 0),
            "after": result.get("after", 0),
            "reason": result.get("reason"),
        })
    elif kind == "models":
        # Live-fetch a provider's full catalog and cache it, then reply with a
        # models event so a GUI picker can show everything the provider offers
        # (not just the built-in list the ready event carried). Defaults to the
        # current provider.
        from .providers import ProviderError, build_provider

        name = cmd.get("provider") or cfg.current_provider
        if not name or name not in cfg.providers:
            emit({"event": "error", "message": f"unknown provider: {name}"})
            return
        entry = dict(cfg.providers[name])
        entry["api_key"] = cfg.resolve_api_key(name)
        try:
            models = build_provider(name, entry).list_models()
        except ProviderError as e:
            # Fall back to the built-in list so the picker still has options.
            emit({"event": "models", "provider": name,
                  "models": _models_for(cfg, name), "ok": False,
                  "error": str(e)})
            return
        cfg.cache_models(name, models)
        emit({"event": "models", "provider": name, "models": models, "ok": True})
    elif kind == "scrum":
        from . import scrum as scrum_mod

        message = scrum_mod.enable() if cmd.get("on", True) else scrum_mod.disable()
        emit({"event": "scrum", "enabled": scrum_mod.is_enabled(), "message": message})
    elif kind == "scrum_action":
        # A GUI board edit (e.g. a user dragging a card): run one scrum()
        # action directly, no model turn involved.
        from . import scrum as scrum_mod

        action = str(cmd.get("action") or "").strip()
        args = {k: v for k, v in cmd.items() if k not in ("type", "action")}
        if not action:
            emit({"event": "error", "message": "scrum_action with no action"})
            return
        message = scrum_mod.scrum({"action": action, **args})
        ok = not message.startswith(("Error:", "Refused:"))
        emit({"event": "scrum_result", "action": action, "message": message, "ok": ok})
    elif kind == "threads":
        emit({"event": "threads", "threads": _thread_summaries()})
    elif kind == "git_status":
        # The GUI polls this while an agent is mid-turn or idle, so it is
        # handled directly (like threads) — never gated on a turn in progress.
        # git_tools returns a clean error dict for not-a-repo / no-git rather
        # than raising, so this branch can never crash the server loop.
        from . import git_tools

        res = git_tools.git_status(str(Path.cwd()))
        if "error" in res:
            emit({"event": "error", "message": res["error"]})
        else:
            emit({
                "event": "git_status",
                "branch": res["branch"],
                "ahead": res["ahead"],
                "behind": res["behind"],
                "files": res["files"],
            })
    elif kind == "git_diff":
        # Like git_status: the GUI asks for a file's diff outside any turn, so
        # this calls git_tools directly. git_tools returns a clean error dict
        # for not-a-repo / no-git / bad-path rather than raising.
        from . import git_tools

        root = Path.cwd()
        raw = str(cmd.get("path") or "").strip()
        if not raw:
            emit({"event": "error", "message": "git_diff command with no path"})
            return
        staged = bool(cmd.get("staged", False))
        # Path traversal guard: the path must resolve INSIDE the project root
        # (../../etc/passwd or an absolute path outside the repo is refused).
        try:
            resolved = (root / raw).resolve(strict=False)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            emit({"event": "error",
                  "message": f"git_diff path escapes the project: {raw}"})
            return
        res = git_tools.git_diff(str(root), path=raw, staged=staged)
        if "error" in res:
            emit({"event": "error", "message": res["error"]})
        else:
            emit({
                "event": "git_diff",
                "path": raw,
                "staged": staged,
                "diff": res["diff"],
                "truncated": res["truncated"],
            })
    elif kind == "resume":
        ident = str(cmd.get("id") or "").strip()
        if not ident:
            emit({"event": "error", "message": "resume command with no id"})
            return
        from . import threads

        t = threads.load_thread(ident)
        if t is None:
            emit({"event": "error", "message": f"no thread matching: {ident}"})
            return
        app._adopt_thread(t, announce=False)
        emit(
            {
                "event": "resumed",
                "thread_id": t.id,
                "title": t.title,
                "message_count": t.message_count,
            }
        )
    elif kind == "confirm":
        emit({"event": "error", "message": "no confirmation is pending"})
    elif kind == "answer":
        emit({"event": "error", "message": "no question is pending"})
    else:
        emit({"event": "error", "message": f"unknown command type: {kind!r}"})


def _thread_summaries() -> list[dict[str, Any]]:
    """Light metadata for every saved thread in this project (newest first)."""
    from . import threads

    return [
        {
            "id": t.id,
            "title": t.title,
            "updated": t.updated,
            "cwd": t.cwd,
            "provider": t.provider,
            "model": t.model,
            "message_count": t.message_count,
        }
        for t in threads.list_threads()
    ]


def _version() -> str:
    from . import __version__

    return __version__
