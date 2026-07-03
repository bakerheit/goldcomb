"""nexais — a provider-agnostic, Claude-Code-style terminal AI agent.

Run ``nexais`` for an interactive session, or ``nexais -p "question"`` for a
one-shot answer. Configure providers and switch models with slash commands.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Config
from .presets import PRESETS, PRESETS_BY_KEY, Preset
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
from .tools import TOOLS_BY_NAME, describe_call, tool_specs

COMMANDS = [
    "help", "setup", "provider", "use", "model", "models", "system", "tools",
    "auto", "set", "clear", "history", "save", "load", "config", "exit",
]

MAX_TOOL_ITERATIONS = 30


class App:
    def __init__(self, cfg: Config, console: Console | None = None):
        self.cfg = cfg
        self.console = console or Console()
        self.messages: list[Message] = []
        self.approved_tools: set[str] = set()
        self.auto_approve = False
        self._last_models: list[str] = []  # remembered from the last /models

    # ---- provider / model helpers -----------------------------------------

    def get_provider(self) -> Provider:
        name = self.cfg.current_provider
        if not name:
            raise ProviderError(
                "No provider configured. Run  /setup  for a guided menu."
            )
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        return build_provider(name, entry)

    def system_prompt(self) -> str | None:
        parts = []
        if self.cfg.settings.get("tools_enabled"):
            cwd = Path.cwd()
            parts.append(
                "You are nexais, a concise AI coding assistant running in the user's "
                f"terminal. The working directory is {cwd}. You can read and write "
                "files and run shell commands via the provided tools. Prefer acting "
                "through tools over describing what you would do. Today's date is "
                f"{date.today().isoformat()}."
            )
        user_sys = self.cfg.settings.get("system_prompt")
        if user_sys:
            parts.append(user_sys)
        return "\n\n".join(parts) if parts else None

    # ---- the agentic turn --------------------------------------------------

    def run_turn(self, user_text: str) -> None:
        self.messages.append(Message(role="user", content=user_text))
        try:
            provider = self.get_provider()
        except ProviderError as e:
            self.console.print(f"[red]{e}[/red]")
            return

        for _ in range(MAX_TOOL_ITERATIONS):
            message, stop = self._stream_once(provider)
            if message is None:
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
        self.console.print("[yellow]Reached maximum tool iterations.[/yellow]")

    def _stream_once(self, provider: Provider):
        tools = tool_specs() if self.cfg.settings.get("tools_enabled") else None
        settings = self.cfg.settings
        text_acc: list[str] = []
        printed_label = False
        completed: Completed | None = None
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
                    if not printed_label:
                        self.console.print(
                            f"[bold cyan]{self.cfg.current_provider}[/bold cyan] "
                            f"[dim]({self.cfg.current_model})[/dim]"
                        )
                        printed_label = True
                    text_acc.append(ev.text)
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
                elif isinstance(ev, ThinkingDelta):
                    pass  # reasoning stream; not shown by default
                elif isinstance(ev, Completed):
                    completed = ev
            if printed_label:
                sys.stdout.write("\n")
                sys.stdout.flush()
        except ProviderError as e:
            if printed_label:
                sys.stdout.write("\n")
            self.console.print(f"[red]Error:[/red] {e}")
            return None, "error"
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            self.console.print("[yellow]⏹ interrupted[/yellow]")
            partial = "".join(text_acc)
            return (Message(role="assistant", content=partial) if partial else None), "interrupted"

        if completed is not None:
            self._show_usage(completed.usage)
            return completed.message, completed.stop_reason
        # No completion event and no text — treat as empty assistant turn.
        return Message(role="assistant", content="".join(text_acc)), "end_turn"

    def _show_usage(self, usage: dict[str, int]) -> None:
        if usage.get("input_tokens") or usage.get("output_tokens"):
            self.console.print(
                f"[dim]  ↳ {usage.get('input_tokens', 0)} in / "
                f"{usage.get('output_tokens', 0)} out tokens[/dim]"
            )

    def _run_tools(self, tool_calls) -> list[Message] | None:
        results: list[Message] = []
        for call in tool_calls:
            tool = TOOLS_BY_NAME.get(call.name)
            summary = describe_call(call.name, call.arguments)
            self.console.print(f"[magenta]⚙ {summary}[/magenta]")
            if tool is None:
                results.append(
                    Message(
                        role="tool",
                        content=f"Error: unknown tool '{call.name}'",
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                continue
            if tool.dangerous and not self.auto_approve and call.name not in self.approved_tools:
                decision = self._confirm(summary)
                if decision == "abort":
                    self.console.print("[yellow]Aborted.[/yellow]")
                    return None
                if decision == "deny":
                    results.append(
                        Message(
                            role="tool",
                            content="User denied this tool call.",
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    continue
                if decision == "always":
                    self.approved_tools.add(call.name)
            try:
                output = tool.run(call.arguments)
            except Exception as e:  # noqa: BLE001 - surface tool errors to the model
                output = f"Error executing tool: {e}"
            self._preview(output)
            results.append(
                Message(role="tool", content=output, tool_call_id=call.id, name=call.name)
            )
        return results

    def _confirm(self, summary: str) -> str:
        self.console.print(
            f"[bold]Run this?[/bold] [dim](y=yes, n=no, a=always this tool, q=abort turn)[/dim]"
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

    def _preview(self, output: str) -> None:
        lines = output.splitlines()
        shown = lines[:12]
        for ln in shown:
            self.console.print(f"[dim]  {ln}[/dim]", highlight=False)
        if len(lines) > len(shown):
            self.console.print(f"[dim]  … (+{len(lines) - len(shown)} lines)[/dim]")

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
            "auto": self.cmd_auto,
            "set": self.cmd_set,
            "clear": self.cmd_clear,
            "history": self.cmd_history,
            "save": self.cmd_save,
            "load": self.cmd_load,
            "config": self.cmd_config,
            "exit": lambda a: False,
            "quit": lambda a: False,
            "q": lambda a: False,
        }.get(cmd)
        if handler is None:
            close = difflib.get_close_matches(cmd, COMMANDS, n=1)
            hint = f"  Did you mean [cyan]/{close[0]}[/cyan]?" if close else "  (try /help)"
            self.console.print(f"[red]Unknown command:[/red] /{cmd}{hint}")
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
                self.console.print("[red]Usage:[/red] /provider remove <name>")
                return
            self.cfg.remove_provider(rest[0])
            self.console.print(f"[green]Removed provider '{rest[0]}'.[/green]")
        elif sub == "use":
            self.cmd_use(rest)
        elif sub == "set":
            if len(rest) < 3:
                self.console.print("[red]Usage:[/red] /provider set <name> <field> <value>")
                return
            name, field, value = rest[0], rest[1], " ".join(rest[2:])
            try:
                self.cfg.set_provider_field(name, field, value)
                self.console.print(f"[green]Set {name}.{field}.[/green]")
            except KeyError:
                self.console.print(f"[red]No such provider:[/red] {name}")
        elif sub == "models":
            self.cmd_models(rest)
        else:
            self.console.print(f"[red]Unknown subcommand:[/red] /provider {sub}")

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
                f"[yellow]Not sure how to add '{first}'.[/yellow]  "
                "Run [cyan]/setup[/cyan] for a menu, or "
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
                f"[red]Unknown type '{rest[1]}'.[/red]  "
                f"Known: {', '.join(sorted(PROVIDER_TYPES))}.  Or run [cyan]/setup[/cyan]."
            )
            return
        api_key = rest[2] if len(rest) > 2 else None
        base_url = rest[3] if len(rest) > 3 else (preset.base_url if preset else None)
        if ptype == "openai-compatible" and not base_url and sys.stdin.isatty():
            base_url = self._ask_line("Base URL (e.g. https://openrouter.ai/api/v1): ").strip() or None
        if not api_key and sys.stdin.isatty():
            api_key = self._ask_secret(f"API key for '{name}' (blank to use env var): ").strip() or None
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
                api_key = self._ask_secret(f"Paste your {preset.label.split(' — ')[0]} API key: ").strip() or None
                if not api_key:
                    self.console.print(
                        "[yellow]No key entered.[/yellow] You can add it later: "
                        f"[dim]/provider set {name} api_key <key>[/dim]"
                    )
        self.cfg.add_provider(name, preset.type, api_key=api_key, base_url=preset.base_url)
        if preset.note:
            self.console.print(f"[dim]{preset.note}[/dim]")
        self._announce_added(name, preset.type, preset.default_model)

    def _add_custom(self) -> None:
        base_url = self._ask_line("Base URL (e.g. http://localhost:8000/v1): ").strip()
        if not base_url:
            self.console.print("[yellow]Cancelled — a base URL is required.[/yellow]")
            return
        name = self._ask_line("Name for this provider [custom]: ").strip() or self._unique_name("custom")
        api_key = self._ask_secret("API key (leave blank if the endpoint needs none): ").strip() or None
        self.cfg.add_provider(name, "openai-compatible", api_key=api_key, base_url=base_url)
        self._announce_added(name, "openai-compatible", "")

    def _announce_added(self, name: str, ptype: str, default_model: str) -> None:
        # Give a fresh provider a sensible model if one wasn't set on add.
        if self.cfg.current_provider == name and default_model and not self.cfg.current_model:
            self.cfg.set_model(default_model)
        self.console.print(f"[green]✓ Added '{name}'[/green] [dim]({ptype})[/dim]")
        if self.cfg.current_provider == name:
            model = self.cfg.current_model or "[yellow]no model yet[/yellow]"
            self.console.print(f"[green]Now using[/green] [cyan]{name}[/cyan] / {model}")

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
            self.console.print("[dim]Pick a model with /models, or set one with /model <name>.[/dim]")

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
            self.console.print(f"  [cyan]{i:>2}[/cyan]  {label}")
        raw = self._ask_line("Enter a number or name (blank to cancel): ").strip().lower()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
            self.console.print("[red]That number isn't on the list.[/red]")
            return None
        keys = [k for k, _ in options]
        if raw in keys:
            return raw
        matches = [k for k, label in options if k.startswith(raw) or label.lower().startswith(raw)]
        if len(matches) == 1:
            return matches[0]
        self.console.print("[red]Didn't recognize that choice.[/red]")
        return None

    def _pick_model_interactive(self, name: str) -> None:
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        provider = build_provider(name, entry)
        self.console.print("[dim]Fetching models…[/dim]")
        try:
            models = provider.list_models()
        except ProviderError as e:
            self.console.print(f"[red]{e}[/red]  [dim]Set one manually with /model <name>.[/dim]")
            return
        if not models:
            self.console.print("[dim]No models returned. Set one with /model <name>.[/dim]")
            return
        self._last_models = models
        for i, m in enumerate(models[:40], 1):
            self.console.print(f"  [cyan]{i:>2}[/cyan]  {m}")
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
            self.console.print(f"[green]Model set to[/green] {sel}")

    def _resolve_model_choice(self, raw: str) -> str | None:
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(self._last_models):
                return self._last_models[idx]
            self.console.print("[red]No model with that number — run /models first.[/red]")
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
                "[yellow]No providers configured.[/yellow] Run [cyan]/setup[/cyan] for a guided menu."
            )
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("", width=2)
        table.add_column("name")
        table.add_column("type")
        table.add_column("key")
        table.add_column("base_url")
        for name, entry in self.cfg.providers.items():
            marker = "→" if name == self.cfg.current_provider else ""
            has_key = "✓" if self.cfg.resolve_api_key(name) else "[red]✗[/red]"
            table.add_row(marker, name, entry.get("type", "?"), has_key, entry.get("base_url", ""))
        self.console.print(table)

    def cmd_use(self, args) -> None:
        if not args:
            self.console.print(r"[red]Usage:[/red] /use <provider> \[model]")
            return
        name = args[0]
        model = args[1] if len(args) > 1 else None
        try:
            self.cfg.use_provider(name, model)
        except KeyError:
            self.console.print(f"[red]No such provider:[/red] {name}")
            return
        self.console.print(
            f"[green]Using[/green] {name} / {self.cfg.current_model or '(no model — set with /model)'}"
        )

    def cmd_model(self, args) -> None:
        if not args:
            self.console.print(
                f"Current: [cyan]{self.cfg.current_provider}[/cyan] / "
                f"[green]{self.cfg.current_model or '(none)'}[/green]"
            )
            if self.cfg.current_provider:
                self.console.print(
                    "[dim]Set with /model <name>, or /models to pick from a numbered list.[/dim]"
                )
            return
        if args[0] == "list":
            ptype = self.cfg.providers.get(self.cfg.current_provider or "", {}).get("type", "")
            models = default_models_for(ptype)
            if models:
                self.console.print("Known models for this provider type:")
                for m in models:
                    self.console.print(f"  {m}")
                self.console.print("[dim]Use /models to query the API for the full live list.[/dim]")
            else:
                self.console.print("[dim]No static model list; try /models.[/dim]")
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
            self.console.print(f"[green]Model set to[/green] {sel}")
        except ValueError as e:
            self.console.print(f"[red]{e}[/red]")

    def cmd_models(self, args) -> None:
        # Args can name a provider and/or a filter substring.
        name = self.cfg.current_provider
        filt: str | None = None
        for a in args:
            if a in self.cfg.providers:
                name = a
            else:
                filt = a
        if not name:
            self.console.print("[red]No provider selected.[/red] Run [cyan]/setup[/cyan].")
            return
        if name not in self.cfg.providers:
            self.console.print(f"[red]No such provider:[/red] {name}")
            return
        entry = dict(self.cfg.providers[name])
        entry["api_key"] = self.cfg.resolve_api_key(name)
        provider = build_provider(name, entry)
        self.console.print(f"[dim]Querying {name} for models…[/dim]")
        try:
            models = provider.list_models()
        except ProviderError as e:
            self.console.print(f"[red]{e}[/red]")
            return
        if filt:
            models = [m for m in models if filt.lower() in m.lower()]
            if not models:
                self.console.print(f"[yellow]No models matching '{filt}'.[/yellow]")
                return
        self._last_models = models
        for i, m in enumerate(models, 1):
            marker = " [green]← current[/green]" if m == self.cfg.current_model else ""
            self.console.print(f"  [cyan]{i:>3}[/cyan]  {m}{marker}")
        self.console.print(
            f"[dim]{len(models)} models — set one with[/dim] [cyan]/model <number>[/cyan]"
        )

    def cmd_system(self, args) -> None:
        if not args:
            cur = self.cfg.settings.get("system_prompt")
            self.console.print(f"System prompt: {cur or '[dim](default agent prompt)[/dim]'}")
            return
        if args[0] == "clear":
            self.cfg.set_setting("system_prompt", None)
            self.console.print("[green]Cleared custom system prompt.[/green]")
            return
        self.cfg.set_setting("system_prompt", " ".join(args))
        self.console.print("[green]System prompt set.[/green]")

    def cmd_tools(self, args) -> None:
        if args and args[0] in ("on", "off"):
            self.cfg.set_setting("tools_enabled", args[0] == "on")
        elif args:
            self.console.print(r"[red]Usage:[/red] /tools \[on|off]")
            return
        state = "on" if self.cfg.settings.get("tools_enabled") else "off"
        self.console.print(f"Tools: [cyan]{state}[/cyan]  ({', '.join(TOOLS_BY_NAME)})")

    def cmd_auto(self, args) -> None:
        if args and args[0] in ("on", "off"):
            self.auto_approve = args[0] == "on"
        else:
            self.auto_approve = not self.auto_approve
        self.console.print(
            f"Auto-approve tool calls: [cyan]{'on' if self.auto_approve else 'off'}[/cyan]"
        )

    def cmd_set(self, args) -> None:
        if len(args) < 2:
            self.console.print("[red]Usage:[/red] /set <max_tokens|temperature> <value>")
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
            self.console.print(f"[red]Unknown setting:[/red] {key}")
            return
        self.console.print(f"[green]{key} = {value}[/green]")

    def cmd_clear(self, args) -> None:
        self.messages.clear()
        self.console.print("[green]Conversation cleared.[/green]")

    def cmd_history(self, args) -> None:
        self.console.print(f"[dim]{len(self.messages)} messages in this conversation[/dim]")
        for m in self.messages:
            role = m.role
            preview = (m.content or "").replace("\n", " ")[:80]
            if m.tool_calls:
                preview += f" [{', '.join(t.name for t in m.tool_calls)}]"
            self.console.print(f"  [bold]{role}:[/bold] {preview}")

    def cmd_save(self, args) -> None:
        path = Path(args[0]) if args else Path("nexais-session.json")
        data = {
            "provider": self.cfg.current_provider,
            "model": self.cfg.current_model,
            "messages": [m.to_dict() for m in self.messages],
        }
        try:
            path.write_text(json.dumps(data, indent=2))
        except OSError as e:
            self.console.print(f"[red]Error saving:[/red] {e}")
            return
        self.console.print(f"[green]Saved {len(self.messages)} messages to {path}[/green]")

    def cmd_load(self, args) -> None:
        path = Path(args[0]) if args else Path("nexais-session.json")
        if not path.exists():
            self.console.print(f"[red]Not found:[/red] {path}")
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            self.console.print(f"[red]Error loading:[/red] {e}")
            return
        self.messages = [Message.from_dict(m) for m in data.get("messages", [])]
        self.console.print(f"[green]Loaded {len(self.messages)} messages from {path}[/green]")

    def cmd_config(self, args) -> None:
        self.console.print(f"Config file: [dim]{self.cfg.path}[/dim]")
        self.console.print(
            f"Current: [cyan]{self.cfg.current_provider}[/cyan] / "
            f"[green]{self.cfg.current_model}[/green]"
        )
        self._print_providers()

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


HELP_TEXT = r"""[bold]nexais commands[/bold]

  [bold cyan]/setup[/bold cyan]                                 guided menu — pick a provider, paste a key
  [cyan]/provider add[/cyan] <preset|name type> …       add a provider (e.g. /provider add openrouter)
  [cyan]/provider list[/cyan]                          show configured providers
  [cyan]/provider remove[/cyan] <name>                 delete a provider
  [cyan]/provider set[/cyan] <name> <field> <value>    edit api_key / base_url
  [cyan]/use[/cyan] <provider> \[model]                 switch active provider
  [cyan]/model[/cyan] \[name|number]                    set the model (number from /models)
  [cyan]/model list[/cyan]                             known models for this provider type
  [cyan]/models[/cyan] \[provider] \[filter]             numbered live model list from the API
  [cyan]/system[/cyan] \[prompt|clear]                  show/set system prompt
  [cyan]/tools[/cyan] \[on|off]                         toggle file/shell tools
  [cyan]/auto[/cyan] \[on|off]                          auto-approve tool calls
  [cyan]/set[/cyan] max_tokens|temperature <value>     tune generation
  [cyan]/clear[/cyan]                                  reset the conversation
  [cyan]/history[/cyan]                                list messages so far
  [cyan]/save[/cyan] \[path] · [cyan]/load[/cyan] \[path]           persist a session
  [cyan]/config[/cyan]                                 show config location
  [cyan]/help[/cyan]  ·  [cyan]/exit[/cyan]

  [dim]presets: anthropic, openai, gemini, openrouter, groq, deepseek, mistral,
  together, ollama, lmstudio  —  e.g.  /provider add groq[/dim]

Type a message to chat. Ctrl-C interrupts a response; Ctrl-D or /exit quits."""


# ---- REPL & entry point -----------------------------------------------------


def _build_prompt_session(app: App):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import NestedCompleter
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return None

    def completer_dict() -> dict:
        provider_names = {n: None for n in app.cfg.providers}
        preset_keys = {p.key: None for p in PRESETS}
        model_names = {m: None for m in app._last_models} or {
            m: None for m in default_models_for(
                app.cfg.providers.get(app.cfg.current_provider or "", {}).get("type", "")
            )
        }
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
            "/auto": {"on": None, "off": None},
            "/set": {"max_tokens": None, "temperature": None},
            "/clear": None,
            "/history": None,
            "/save": None,
            "/load": None,
            "/config": None,
            "/exit": None,
            "/quit": None,
        }

    hist_path = app.cfg.path.parent / "history"
    session = PromptSession(history=FileHistory(str(hist_path)))

    def bottom_toolbar():
        p = app.cfg.current_provider or "no-provider"
        m = app.cfg.current_model or "no-model"
        t = "tools" if app.cfg.settings.get("tools_enabled") else "no-tools"
        return f" {p}:{m} | {t} "

    def prompt() -> str:
        return session.prompt(
            "› ",
            completer=NestedCompleter.from_nested_dict(completer_dict()),
            bottom_toolbar=bottom_toolbar,
        )

    return prompt


def repl(app: App) -> None:
    console = app.console
    console.print(
        Panel.fit(
            "[bold]nexais[/bold] — multi-provider AI agent for the terminal\n"
            "[dim]Type /help for commands, /exit to quit.[/dim]",
            border_style="cyan",
        )
    )
    prompt = _build_prompt_session(app)

    if app.cfg.current_provider:
        console.print(
            f"[dim]Using[/dim] [cyan]{app.cfg.current_provider}[/cyan] / "
            f"[green]{app.cfg.current_model or '(no model set — /model <name>)'}[/green]"
        )
    elif sys.stdin.isatty():
        console.print("[yellow]No provider configured yet — let's add one.[/yellow]")
        app.cmd_setup([])
    else:
        console.print(
            "[yellow]No provider configured.[/yellow] Run [cyan]/setup[/cyan]."
        )
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
            if not app.handle_command(line):
                console.print("[dim]bye[/dim]")
                break
            continue
        try:
            app.run_turn(line)
        except KeyboardInterrupt:
            console.print("\n[yellow]⏹ interrupted[/yellow]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nexais",
        description="A provider-agnostic, Claude-Code-style terminal AI agent.",
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt (non-interactive).")
    parser.add_argument("-p", "--print", dest="oneshot", metavar="PROMPT",
                        help="Answer a single prompt and exit.")
    parser.add_argument("--provider", help="Override the active provider for this run.")
    parser.add_argument("--model", help="Override the active model for this run.")
    parser.add_argument("--no-tools", action="store_true", help="Disable file/shell tools.")
    parser.add_argument("-V", "--version", action="version",
                        version=f"nexais {__version__}")
    args = parser.parse_args(argv)

    cfg = Config.load()
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

    console = Console()
    app = App(cfg, console)

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
            print("No provider configured. Run `nexais` and use /setup.", file=sys.stderr)
            return 2
        app.auto_approve = True  # non-interactive: don't block on confirmations
        try:
            app.run_turn(oneshot)
        except KeyboardInterrupt:
            return 130
        return 0

    try:
        repl(app)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
