# Working on scars

scars is a local Python CLI that mines Claude Code and Codex transcripts for failure-then-fix pairs and delivers the resulting lessons through agent hooks. `src/scars/model.py` holds the shared domain types, and `src/scars/schema/` holds the SQLite schema.

## Commands

- Set up: `uv sync`
- Lint: `uv run ruff check` and `uv run ruff format --check`
- Test: `uv run pytest`

Run all three before every push. CI runs the same commands on macOS and Linux with Python 3.11 and 3.13.

## Rules

- The runtime has no third-party dependencies. Only the standard library ships. Dev tools go in `[dependency-groups]`.
- Python 3.11 is the floor. Use `X | None`, `match`, dataclasses and enums.
- Code lives in `src/scars/`. Tests live in `tests/` and mirror the module they cover.
- Parse external input (hook stdin, transcripts, CLI args, files) into typed records at the boundary. Inside the package, trust the types.
- Tests call code the way users do and assert literal expected values. A test that would still pass if the function under test returned `None` gets rewritten or deleted.
- Keep a comment only when it explains a non-obvious why.
- PR titles use Conventional Commits, for example `feat(store): add compare-and-set lesson updates`.
