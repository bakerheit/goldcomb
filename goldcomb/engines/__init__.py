"""Alternative execution engines.

goldcomb's default (``native``) engine is the agentic tool loop in ``cli.py``:
it calls ``Provider.stream`` and runs goldcomb's own ``tools.py`` tools. The
``claude`` engine here is an opt-in alternative that hands a turn to the real
Claude Code harness via the Claude Agent SDK — Anthropic-only, its own built-in
tools. See ``engines/claude.py`` and ``ENGINES``.
"""

from __future__ import annotations

#: Engine names accepted by ``--engine`` / ``/mode``. ``native`` is the default
#: provider-agnostic loop; ``claude`` delegates to the Claude Agent SDK.
ENGINES = ("native", "claude")

__all__ = ["ENGINES"]
