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

from scars import paths, render, store, telemetry
from scars.model import Lesson, LessonId


class UserError(Exception):
    """Something the person running the command can fix. The message is one line."""


HARNESS_NAMES = frozenset({"claude", "claude-code", "codex", "codex-cli"})
"""Executable names of the agent harnesses. A privileged command refuses to run under any of them."""
HARNESS_MARKERS = ("@anthropic-ai/claude-code", "@openai/codex", "/claude-code/", "/codex/bin/")
"""Command-line fragments that identify a harness run through an interpreter such as node."""
HARNESS_ENV = ("CLAUDECODE", "CODEX_SANDBOX")
ANCESTOR_DEPTH = 12
PS = next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.exists(p)), "/bin/ps")
"""`ps` by absolute path: a PATH lookup would let the caller swap in a `ps` that hides the harness."""


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


def process_ancestors(pid: int | None = None) -> list[str] | None:
    """The command lines of `pid` (default: this process's parent) and its ancestors, nearest first.

    None when the chain cannot be read: `ps` is missing, fails, prints nothing, or prints something
    that does not parse. A caller that needs the chain must treat None as a reason to refuse.
    """
    out: list[str] = []
    pid = os.getppid() if pid is None else pid
    for _ in range(ANCESTOR_DEPTH):
        if pid <= 1:
            break
        try:
            result = subprocess.run(
                [PS, "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=2
            )
        except (OSError, subprocess.SubprocessError):
            return None
        found = result.stdout.strip()
        if result.returncode != 0 or not found:
            return None
        parent, _, command = found.partition(" ")
        try:
            pid = int(parent)
        except ValueError:
            return None
        out.append(command.strip())
    return out or None


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
    ancestors: Callable[[], list[str] | None] = process_ancestors
    """Command lines of the ancestor processes, nearest first; None when they cannot be read."""
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
    stdout are terminals, `CLAUDECODE` and `CODEX_SANDBOX` are absent (even set to an empty string they
    mark an agent session), and the ancestor processes can be read and none is an agent harness. When the
    ancestry cannot be read the answer is no, not yes. An agent determined to get around this can, so it
    is a speed bump; the hook's agent deny rule is the second layer.
    """
    for name in HARNESS_ENV:
        if name in ctx.env:
            return f"{name} is set, so this looks like an agent session; run it in your own terminal"
    if not (_is_tty(ctx.stdin) and _is_tty(ctx.stdout)):
        return "stdin and stdout must be a terminal; run it in your own terminal"
    ancestors = ctx.ancestors()
    if not ancestors:
        return "cannot tell which processes started it; run it in your own terminal"
    for command_line in ancestors:
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


READ_BUSY_TIMEOUT_MS = 250


def _probe(uri: str, table: str) -> sqlite3.Connection:
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        db.execute(f"pragma busy_timeout = {READ_BUSY_TIMEOUT_MS}")
        db.execute(f"select 1 from {table} limit 0")
    except BaseException:
        db.close()
        raise
    return db


def connect_readonly(path: Path, table: str) -> sqlite3.Connection:
    """Open a store file read-only, the way the hook does, checking that `table` is readable.

    A WAL database in a directory this process cannot write (an agent sandbox that denies writes to
    `~/.scars`) cannot be opened normally, because SQLite has to create the `-shm` file. When the
    directory is not writable and there is no `-wal` file, no writer can have changes pending, so the
    file is opened as immutable instead.
    """
    uri = telemetry.uri(os.path.abspath(path), "ro")
    try:
        return _probe(uri, table)
    except sqlite3.Error:
        if os.access(path.parent, os.W_OK) or os.path.exists(f"{path}-wal"):
            raise
        return _probe(uri + "&immutable=1", table)


@contextmanager
def lessons_db(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """lessons.db for one command. Read-only commands pass `readonly`, so they never take a write lock."""
    path = require_home() / "lessons.db"
    try:
        db = connect_readonly(path, "lessons") if readonly else store.connect(path)
    except sqlite3.Error as exc:
        raise UserError(f"cannot open {path}: {exc}") from None
    db.row_factory = sqlite3.Row
    try:
        yield db
    except sqlite3.Error as exc:
        raise UserError(f"cannot read {path}: {exc}") from None
    finally:
        db.close()


def get_lesson(db: sqlite3.Connection, lesson_id: str) -> Lesson:
    lesson = store.get_lesson(db, LessonId(lesson_id))
    if lesson is None:
        raise UserError(f"no lesson {lesson_id}")
    return lesson


def telemetry_db() -> sqlite3.Connection | None:
    """telemetry.db read-only, or None when it is missing or unreadable: telemetry is best-effort."""
    path = paths.home() / "telemetry.db"
    if not path.exists():
        return None
    try:
        return connect_readonly(path, "hits")
    except sqlite3.Error:
        return None


DISPLAY_RULES = (*render.SANITIZER_RULES, *render.IMPERATIVE_RULES, *render.SECRET_RULES)
"""The lesson text rules, minus the observed-command rule (which is about a lesson's trust class)."""


def shown(value: str) -> str:
    """`value`, or `[hidden: <rule>]` when it breaks a lesson text rule.

    Agents run the read commands too, so whatever they print is a way into an agent's context. Stored
    values (lesson text, trigger specs, mined inputs, observations, event payloads) came from transcripts
    and are held to the same rules as lesson text before they are printed.
    """
    if value == "":
        return value
    for rule in DISPLAY_RULES:
        if rule.hits(value):
            return f"[hidden: {rule.name}]"
    return value


def _shown_leaves(value: object) -> object:
    match value:
        case str():
            return shown(value)
        case list():
            return [_shown_leaves(v) for v in value]
        case dict():
            return {shown(str(k)): _shown_leaves(v) for k, v in value.items()}
    return value


def shown_json(payload: str) -> str:
    """A stored JSON payload with every string in it passed through `shown`, as compact JSON."""
    import json

    try:
        value = json.loads(payload)
    except ValueError:
        return shown(payload)
    return json.dumps(_shown_leaves(value), separators=(",", ":"), ensure_ascii=False)
