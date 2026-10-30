"""What every command shares: the injected terminal, user errors, the store, and the privileged check.

A command handler takes the parsed arguments and a `Context`, prints plain text for people on
`ctx.stdout`, and returns 0. A user error raises `UserError`, which `scars.cli` turns into one line on
stderr and exit code 1.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from scars import paths, store
from scars.model import Lesson, LessonId


class UserError(Exception):
    """Something the person running the command can fix. The message is one line."""


HARNESS_NAMES = frozenset({"claude", "claude-code", "codex", "codex-cli"})
"""Executable names of the agent harnesses. A privileged command refuses to run under any of them."""
HARNESS_MARKERS = ("@anthropic-ai/claude-code", "@openai/codex", "/claude-code/", "/codex/bin/")
"""Command-line fragments that identify a harness run through an interpreter such as node."""
HARNESS_ENV = ("CLAUDECODE", "CODEX_SANDBOX")
ANCESTOR_DEPTH = 12


def _read_key(stream: TextIO) -> str:
    """One keypress from a terminal, without waiting for Enter."""
    import termios
    import tty

    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return os.read(fd, 1).decode(errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def process_ancestors(pid: int | None = None) -> list[str]:
    """The command lines of this process's ancestors, nearest first, as `ps` reports them."""
    out: list[str] = []
    pid = os.getppid() if pid is None else pid
    for _ in range(ANCESTOR_DEPTH):
        if pid <= 1:
            break
        try:
            found = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=2
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not found:
            break
        parent, _, command = found.partition(" ")
        out.append(command.strip())
        try:
            pid = int(parent)
        except ValueError:
            break
    return out


def edit_in_editor(text: str, env: Mapping[str, str]) -> str:
    """Open `text` in `$VISUAL` or `$EDITOR` (vi when neither is set) and return what was saved."""
    import shlex
    import tempfile

    editor = env.get("VISUAL") or env.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        result = subprocess.run([*shlex.split(editor), name])
        if result.returncode != 0:
            raise UserError(f"the editor exited with code {result.returncode}; nothing changed")
        return Path(name).read_text()
    finally:
        os.unlink(name)


@dataclass
class Context:
    """The outside world a command touches. Tests replace any part of it."""

    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    read_key: Callable[[], str] | None = None
    """Returns one keypress. None reads from `stdin` in cbreak mode."""
    ancestors: Callable[[], list[str]] = process_ancestors
    edit: Callable[[str], str] | None = None
    """Lets the person rewrite a text and returns it. None runs `$EDITOR`."""
    clock: Callable[[], float] = time.time
    user_home: Path = field(default_factory=Path.home)
    platform: str = sys.platform

    def print(self, *parts: object) -> None:
        print(*parts, file=self.stdout)

    def key(self) -> str:
        return self.read_key() if self.read_key is not None else _read_key(self.stdin)

    def ask(self, question: str) -> str:
        """Print a question and read one line of answer; empty at end of input."""
        self.stdout.write(question)
        self.stdout.flush()
        return self.stdin.readline().strip()

    def confirm(self, question: str) -> bool:
        return self.ask(f"{question} [y/N] ").lower() in ("y", "yes")

    def rewrite(self, text: str) -> str:
        return self.edit(text) if self.edit is not None else edit_in_editor(text, self.env)


def _is_tty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def harness_in(command_line: str) -> bool:
    words = command_line.split()
    if words and os.path.basename(words[0]).lstrip("-") in HARNESS_NAMES:
        return True
    return any(marker in command_line for marker in HARNESS_MARKERS)


def refusal(ctx: Context) -> str | None:
    """Why a privileged command must not run here, or None when a person is at a terminal.

    Privileged commands change which lessons reach agents, so they need a human. The checks: stdin and
    stdout are terminals, `CLAUDECODE` and `CODEX_SANDBOX` are unset, and no ancestor process is an agent
    harness. An agent determined to get around this can, so it is a speed bump; the hook's agent deny
    rule is the second layer.
    """
    for name in HARNESS_ENV:
        if ctx.env.get(name):
            return f"{name} is set, so this looks like an agent session; run it in your own terminal"
    if not (_is_tty(ctx.stdin) and _is_tty(ctx.stdout)):
        return "stdin and stdout must be a terminal; run it in your own terminal"
    for command_line in ctx.ancestors():
        if harness_in(command_line):
            return "it was started from an agent harness; run it in your own terminal"
    return None


def require_human(ctx: Context) -> None:
    if (reason := refusal(ctx)) is not None:
        raise UserError(f"refused: {reason}")


def require_home() -> Path:
    home = paths.home()
    if not (home / "lessons.db").exists():
        raise UserError(f"no scars store at {home}; run scars init")
    return home


@contextmanager
def lessons_db() -> Iterator[sqlite3.Connection]:
    db = store.connect(require_home() / "lessons.db")
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def get_lesson(db: sqlite3.Connection, lesson_id: str) -> Lesson:
    lesson = store.get_lesson(db, LessonId(lesson_id))
    if lesson is None:
        raise UserError(f"no lesson {lesson_id}")
    return lesson


def telemetry_db() -> sqlite3.Connection | None:
    path = paths.home() / "telemetry.db"
    if not path.exists():
        return None
    return store.connect(path, readonly=True)
