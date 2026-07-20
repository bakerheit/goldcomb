# Contributing to goldcomb

Thank you for considering contributing!

## Setup

- Python >=3.10 is required.
- Use `pip install -e .` (run from repo root) for editable/dev installs.
- Run tests with `pytest` (see `tests/README.md`).
- Code style: run `flake8` (config in `.flake8`), and keep lines ≤100 chars.

## Coding conventions

- Use explicit type hints for all public function signatures.
- When adding a new tool, define a TypedDict for its arguments in `tools_types.py`.
- For new providers, subclass Provider and integrate in `goldcomb/providers/` and the CLI registry.
- Keep user-facing error messages clear and actionable.
- All code changes should come with tests if possible.
- Significant new capabilities or fixes: add a note to CHANGELOG (or if absent, note in PR).

## Documentation
- Update `README.md` and help output for user-visible changes.
- Add comments/docstrings for new public methods/classes.

## Linting
- Lint with `flake8`: `flake8 .`

## Pull requests

- Open a PR with a description of your change.
- If adding a feature, mention what user command(s)/flags/scripts it impacts.
- If fixing a bug, include a minimal test/repro if possible.
