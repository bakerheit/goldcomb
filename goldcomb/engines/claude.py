"""The ``claude`` engine — drive a turn with the real Claude Code harness.

Instead of goldcomb's own tool loop, this hands the turn to the **Claude Agent
SDK** (``claude-agent-sdk``), which bundles the Claude Code CLI and runs its own
agentic loop with its own built-in tools (Read/Write/Edit/Bash/Glob/Grep/…) in
the working directory. goldcomb only sees the streamed result.

Design: ``ClaudeEngine`` implements the same ``Provider.stream`` contract as the
real providers, but emits a *single* assistant turn with **no** ``tool_calls``
(the SDK executes tools itself). So the existing ``App._drive_turn`` loop runs
it unchanged — one ``stream`` call, one assistant message, done. Tool activity
is surfaced as inline text so the user can watch Claude Code work.

Two seams keep this testable without the SDK (or a network/account):
- ``query_fn`` is injectable; the default lazily imports ``claude_agent_sdk``.
- message → event mapping matches SDK types by class *name*, so tests pass
  lightweight fakes named ``AssistantMessage`` / ``TextBlock`` / etc.

Auth note: the SDK authenticates the way Claude Code does — an
``ANTHROPIC_API_KEY`` in the environment, or Claude Code's own cached login for
a Pro/Max subscription. It does **not** accept goldcomb's ``/login`` OAuth token
programmatically, so claude-mode subscription use rides on a separate Claude
Code login. We pass the provider's API key through the subprocess env when set.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from typing import Any, Callable, Iterator

from ..providers.anthropic import AnthropicProvider
from ..providers.base import (
    Completed,
    Event,
    Message,
    Provider,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolSpec,
)

#: Shown when the SDK isn't installed. Kept as a constant so the CLI can print
#: the same guidance from ``/mode`` before a turn is ever attempted.
SDK_MISSING_MSG = (
    "Claude mode needs the Claude Agent SDK, which isn't installed.\n"
    "  pip install claude-agent-sdk\n"
    "Then retry, or switch back with  /mode native."
)

#: How many prior turns to fold into the prompt as context. The SDK's one-shot
#: ``query`` keeps no memory between calls, so we restate recent conversation
#: (full cross-turn memory via a persistent client is a follow-up).
_CONTEXT_TURNS = 6

#: Bound the SDK's internal agentic loop so a runaway can't spin forever.
_DEFAULT_MAX_TURNS = 40

#: SDK message/block class names we understand (matched by name, not import).
_ASSISTANT = "AssistantMessage"
_RESULT = "ResultMessage"
_TEXT_BLOCK = "TextBlock"
_THINKING_BLOCK = "ThinkingBlock"
_TOOL_USE_BLOCK = "ToolUseBlock"

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def sdk_available() -> bool:
    """True if ``claude-agent-sdk`` can be imported (cheap probe for ``/mode``)."""
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:  # pragma: no cover - import machinery varies by env
        return False
    return True


def build_prompt(messages: list[Message], context_turns: int = _CONTEXT_TURNS) -> str:
    """Render the turn's prompt for the SDK.

    The latest user message is the request; a few prior user/assistant turns are
    prepended as plain-text context, since ``query`` is stateless per call.
    """
    if not messages:
        return ""
    latest = messages[-1]
    request = latest.content if latest.role == "user" else ""
    prior = [
        m for m in messages[:-1] if m.role in ("user", "assistant") and m.content
    ]
    if not prior:
        return request
    lines = []
    for m in prior[-context_turns:]:
        who = "User" if m.role == "user" else "Assistant"
        lines.append(f"{who}: {m.content}")
    context = "\n\n".join(lines)
    return (
        "[Earlier conversation for context]\n"
        f"{context}\n\n"
        "[Current request]\n"
        f"{request}"
    )


def _fmt_tool(name: str, tool_input: Any) -> str:
    """A compact one-line note for a tool the SDK is about to run."""
    detail = ""
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "command", "pattern", "query"):
            val = tool_input.get(key)
            if val:
                detail = f" {str(val).splitlines()[0][:80]}"
                break
    return f"\n`↳ {name}{detail}`\n"


def _usage_dict(usage: Any) -> dict[str, int]:
    """Normalize the SDK's ``ResultMessage.usage`` (object or dict) to ints."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        get = usage.get
    else:
        def get(key: str) -> Any:
            return getattr(usage, key, None)
    out: dict[str, int] = {}
    for key in _USAGE_KEYS:
        val = get(key)
        if isinstance(val, int):
            out[key] = val
    return out


def map_message(msg: Any) -> Iterator[tuple[str, Any]]:
    """Map one SDK message to ``(kind, payload)`` items.

    ``kind`` is one of ``text`` (assistant content, saved), ``thinking``,
    ``activity`` (tool note, shown but not saved), or ``usage``. Matching is by
    class name so tests need no real SDK types.
    """
    cls = type(msg).__name__
    if cls == _ASSISTANT:
        for block in getattr(msg, "content", None) or []:
            bcls = type(block).__name__
            if bcls == _TEXT_BLOCK:
                text = getattr(block, "text", "")
                if text:
                    yield ("text", text)
            elif bcls == _THINKING_BLOCK:
                yield ("thinking", getattr(block, "thinking", "") or "")
            elif bcls == _TOOL_USE_BLOCK:
                yield ("activity", _fmt_tool(
                    getattr(block, "name", "tool"), getattr(block, "input", None)))
    elif cls == _RESULT:
        yield ("usage", _usage_dict(getattr(msg, "usage", None)))


def _describe_sdk_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "CLINotFoundError":
        return (
            "Claude mode couldn't find the Claude Code CLI it bundles. "
            "Reinstall the SDK:  pip install -U claude-agent-sdk"
        )
    if name in ("CLIConnectionError", "ProcessError", "CLIJSONDecodeError"):
        return f"Claude mode failed talking to the Claude Code CLI: {exc}"
    return f"Claude mode error: {exc}"


def run_async_stream(
    agen_factory: Callable[[], Any],
    mapper: Callable[[Any], Iterator[tuple[str, Any]]],
) -> Iterator[tuple[str, Any]]:
    """Drive an async message stream from sync code, yielding mapped items.

    Runs ``agen_factory()`` (an async iterator) on a worker thread with its own
    event loop, funnelling ``mapper``'d items through a queue to this generator.
    A ``ProviderError`` (or any error, wrapped as one) is re-raised in the
    caller's thread. Purely mechanical and SDK-agnostic, so it's unit-tested
    directly with fakes.
    """
    q: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    def worker() -> None:
        async def go() -> None:
            try:
                agen = agen_factory()
                async for msg in agen:
                    for item in mapper(msg):
                        q.put(("item", item))
            except BaseException as exc:  # noqa: BLE001 - surfaced to caller below
                q.put(("error", exc))
            finally:
                q.put(("done", None))

        try:
            asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001 - loop setup failure
            q.put(("error", exc))
            q.put(("done", None))

    thread = threading.Thread(target=worker, name="claude-engine", daemon=True)
    thread.start()
    try:
        while True:
            tag, val = q.get()
            if tag == "item":
                yield val
            elif tag == "error":
                if isinstance(val, ProviderError):
                    raise val
                raise ProviderError(_describe_sdk_error(val))
            else:  # done
                return
    finally:
        thread.join(timeout=1.0)


class ClaudeEngine(Provider):
    """Provider-shaped adapter over the Claude Agent SDK (the ``claude`` engine).

    Built directly (not via the provider registry) by ``App.get_provider`` when
    the engine is ``claude``. It reuses Anthropic's model list for completion.
    """

    type_name = "claude-agent-sdk"
    default_models = list(AnthropicProvider.default_models)

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        *,
        cwd: str | None = None,
        auto_approve: bool = False,
        query_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(name, config)
        self._cwd = cwd or _safe_cwd()
        self._auto_approve = auto_approve
        self._query_fn = query_fn

    # -- provider interface --------------------------------------------------

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[Event]:
        # tools / max_tokens / temperature are deliberately ignored: the SDK
        # owns its own tool set and sampling. goldcomb's tool loop sees a single
        # turn with no tool_calls, so it does not re-drive.
        query_fn = self._resolve_query()
        prompt = build_prompt(messages)
        options = self._build_options(model=model, system=system)

        text_parts: list[str] = []
        usage: dict[str, int] = {}

        def agen_factory() -> Any:
            return query_fn(prompt=prompt, options=options)

        for kind, payload in run_async_stream(agen_factory, map_message):
            if kind == "text":
                text_parts.append(payload)
                yield TextDelta(payload)
            elif kind == "activity":
                yield TextDelta(payload)  # shown live, not saved into the message
            elif kind == "thinking":
                yield ThinkingDelta(payload)
            elif kind == "usage":
                usage = payload

        yield Completed(
            message=Message(role="assistant", content="".join(text_parts)),
            stop_reason="end_turn",
            usage=usage,
        )

    def list_models(self) -> list[str]:
        return list(self.default_models)

    # -- internals -----------------------------------------------------------

    def _resolve_query(self) -> Callable[..., Any]:
        if self._query_fn is not None:
            return self._query_fn
        try:
            from claude_agent_sdk import query
        except Exception as exc:  # pragma: no cover - import failure path
            raise ProviderError(SDK_MISSING_MSG) from exc
        return query

    def _permission_mode(self) -> str:
        # Slice 1 has no interactive per-tool bridge to the SDK, so pick a mode
        # that won't hang waiting on a callback: auto-approve edits normally,
        # everything under --sudo/--auto. Interactive approval is a follow-up.
        override = self.config.get("claude_permission_mode")
        if override:
            return str(override)
        return "bypassPermissions" if self._auto_approve else "acceptEdits"

    def _subprocess_env(self) -> dict[str, str] | None:
        """Env for the SDK subprocess: inherit ours, add the API key if set.

        Merging with ``os.environ`` (rather than replacing it) preserves PATH
        and Claude Code's own on-disk subscription login, which the CLI reads
        from ``~/.claude`` regardless of these vars.
        """
        overrides: dict[str, str] = {}
        key = self.config.get("api_key")
        if key:
            overrides["ANTHROPIC_API_KEY"] = str(key)
        base = self.config.get("base_url")
        if base:
            overrides["ANTHROPIC_BASE_URL"] = str(base)
        if not overrides:
            return None
        return {**os.environ, **overrides}

    def _build_options(self, *, model: str, system: str | None) -> Any:
        params: dict[str, Any] = {
            "cwd": self._cwd,
            "permission_mode": self._permission_mode(),
            "max_turns": int(self.config.get("claude_max_turns") or _DEFAULT_MAX_TURNS),
        }
        if model:
            params["model"] = model
        if system:
            params["system_prompt"] = system
        env = self._subprocess_env()
        if env is not None:
            params["env"] = env
        try:
            from claude_agent_sdk import ClaudeAgentOptions
        except Exception:  # pragma: no cover - injected query_fn path (tests)
            return params  # a fake query_fn ignores options; a dict is enough
        return ClaudeAgentOptions(**params)


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:  # pragma: no cover - cwd removed underfoot
        return "."
