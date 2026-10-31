"""What every command shares: the injected terminal, user errors, the store, and the privileged check.

A command handler takes the parsed arguments and a `Context`, prints plain text for people on
`ctx.stdout`, and returns 0. A user error raises `UserError`, which `scars.cli` turns into one line on
stderr and exit code 1.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from scars import paths, render, store, telemetry
from scars.model import ACTIVE_STATUSES, Lesson, LessonId


class UserError(Exception):
    """Something the person running the command can fix. The message is one line."""


HARNESS_NAMES = frozenset({"claude", "claude-code", "codex", "codex-cli", "remy"})
"""Executable names (lowercased, without a `.exe`, `.js` or `.py` suffix) of the agent harnesses."""
_HARNESS_NAME = re.compile(r"codex-(?:aarch64|x86_64|arm64)-[a-z0-9_-]+")
"""Codex's per-platform binary names, such as Homebrew's `codex-aarch64-apple-darwin`."""
HARNESS_INSTALL_DIRS = (
    "/node_modules/@anthropic-ai/claude-code/",
    "/node_modules/@openai/codex/",
    "/node_modules/@openai/codex-",
    "/.local/share/claude/versions/",
    "/.claude/local/",
    "/Caskroom/claude-code/",
    "/Caskroom/codex/",
)
"""Install directories of Claude Code (npm, native installer, Homebrew cask) and Codex (npm and its
per-platform packages, Homebrew cask). The native installer names its binary after the version."""
HARNESS_APPS = ("Remy", "Claude", "Codex", "CodexCLI", "ChatGPT")
"""macOS app bundles that run agents: Remy, Claude Code in the Claude app, the Codex app and the
ChatGPT app's bundled Codex CLI."""
_HARNESS_SHAPES = (
    *(rf"{app}\.app/Contents/(?:MacOS|Resources)/" for app in HARNESS_APPS),
    *(re.escape(d.removeprefix("/")) for d in HARNESS_INSTALL_DIRS),
)
_EXECUTABLE_SHAPE = re.compile(r"^(?:/(?:[^/\s]|\s(?!/))+)*/(?:" + "|".join(_HARNESS_SHAPES) + ")")
"""A command line whose executable lies inside a harness app bundle or install directory, matched on the
whole line because `ps` prints the executable first and joins the arguments with spaces. A directory
name may hold spaces (`/Users/Jane Doe`), but a space followed by `/` starts the next argument, and a
harness path there (`vim /Applications/Claude.app/...`) must not count."""
INTERPRETERS = re.compile(r"(?:node|nodejs|bun|deno|python[0-9.]*)")
_VALUE_OPTIONS = frozenset({"-r", "--require", "--import", "--loader", "-W", "-X", "--config"})
_NO_SCRIPT = frozenset({"-e", "--eval", "-p", "--print", "-c"})
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
    that does not parse, or the walk stops after `ANCESTOR_DEPTH` processes before reaching pid 1 or 0.
    A partial chain is no answer: nesting waiting shells would push a harness out of reach. A caller
    that needs the chain must treat None as a reason to refuse.
    """
    out: list[str] = []
    pid = os.getppid() if pid is None else pid
    for _ in range(ANCESTOR_DEPTH):
        if pid <= 1:
            break
        try:
            result = subprocess.run(
                [PS, "-ww", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=2
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
    if pid > 1:
        return None
    return out or None


def edit_in_editor(text: str, env: Mapping[str, str]) -> str:
    """Open `text` in `$VISUAL` or `$EDITOR` (vi when neither is set) and return what was saved."""
    import shlex
    import tempfile

    editor = env.get("VISUAL") or env.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        name = f.name
    try:
        result = subprocess.run([*shlex.split(editor), name])
        if result.returncode != 0:
            raise UserError(f"the editor exited with code {result.returncode}; nothing changed")
        return Path(name).read_text(encoding="utf-8")
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

    def guarded(self) -> Context:
        """This context with stdout and stderr passed line by line through `clean`."""
        return replace(self, stdout=CleanLines(self.stdout), stderr=CleanLines(self.stderr))


def _is_tty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _is_harness_file(path: str) -> bool:
    name = os.path.basename(path).lstrip("-").lower()
    for suffix in (".exe", ".js", ".mjs", ".cjs", ".py"):
        name = name.removesuffix(suffix)
    if name in HARNESS_NAMES or _HARNESS_NAME.fullmatch(name):
        return True
    return any(d in path for d in HARNESS_INSTALL_DIRS)


def _script_of(args: list[str]) -> str | None:
    """The script or module an interpreter runs: its first argument that is not an option."""
    words = iter(args)
    for word in words:
        if word in _NO_SCRIPT:
            return None
        if word == "-m":
            return next(words, None)
        if word in _VALUE_OPTIONS:
            next(words, None)
        elif not word.startswith("-"):
            return word
    return None


def harness_in(command_line: str) -> bool:
    """Whether the program a command line runs is an agent harness.

    Only the program's identity counts: an executable inside a harness app bundle, the first word's
    name or install directory, or for an interpreter (node, bun, deno, python) the script or module it
    runs. A harness name anywhere else, such as a file being edited, does not.
    """
    if _EXECUTABLE_SHAPE.match(command_line):
        return True
    words = command_line.split()
    if not words:
        return False
    if _is_harness_file(words[0]):
        return True
    if INTERPRETERS.fullmatch(os.path.basename(words[0]).lower()):
        script = _script_of(words[1:])
        return script is not None and _is_harness_file(script)
    return False


def refusal(ctx: Context) -> str | None:
    """Why a privileged command must not run here, or None when a person is at a terminal.

    Privileged commands change which lessons reach agents, so they need a human. The checks: stdin and
    stdout are terminals, `CLAUDECODE` and `CODEX_SANDBOX` are absent (even set to an empty string they
    mark an agent session), and the ancestor processes can be read and none is an agent harness. When the
    ancestry cannot be read, or is deeper than the walk, the answer is no, not yes.

    This is a speed bump, not a security boundary. An agent running as the same user with a shell can
    always defeat ancestry detection (by detaching from its harness, for example), so it only stops an
    agent that runs the command directly. The real boundary is the harness's sandbox, which should deny
    writes to the scars home; the hook's agent deny rule is a further layer.
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
    """The lesson, or a `UserError` when there is none or its stored row does not load."""
    try:
        lesson = store.get_lesson(db, LessonId(lesson_id))
    except (ValueError, TypeError) as exc:
        raise UserError(f"lesson {shown(lesson_id)} cannot be read: {shown(str(exc))}") from None
    if lesson is None:
        raise UserError(f"no lesson {shown(lesson_id)}")
    return lesson


_BUDGET_CHARS = {f"budget.{stage}": b.chars for stage, b in render.STAGE_BUDGETS.items()} | {
    "budget.session_start": render.SESSION_START_BUDGET.chars
}


def rejection(exc: render.RejectedText) -> str:
    """Why `render` refused a lesson, in words for the person who wrote it."""
    chars = _BUDGET_CHARS.get(exc.rule)
    if chars is None:
        return f"{exc.field} breaks {exc.rule}"
    stage = exc.rule.removeprefix("budget.").replace("_", " ")
    return f"the lesson is over the {stage} budget of {chars} characters"


def unrenderable(db: sqlite3.Connection, lessons: list[Lesson]) -> list[tuple[LessonId, render.RejectedText]]:
    """Each active lesson in `lessons` that `render` refuses, with the rule it breaks, so no lesson
    drops out of delivery without a word."""
    active = [le for le in lessons if le.status in ACTIVE_STATUSES]
    known = store.passing_inputs(db, [le.id for le in active])
    repos = store.repo_names(db)
    found = []
    for lesson in active:
        problem = render.render_problem(lesson, known_commands=known[lesson.id], registered_repos=repos)
        if problem is not None:
            found.append((lesson.id, problem))
    return found


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

_LINE_SANITIZER = ("sanitizer.ansi_escape", "sanitizer.control_char", "sanitizer.non_ascii")
LINE_RULES = (*(r for r in render.SANITIZER_RULES if r.name in _LINE_SANITIZER), *render.SECRET_RULES)
"""What every printed line of a read command must pass: printable ASCII only, and no secret."""


def clean(line: str) -> str:
    """`line`, or `[hidden: <rule>]` when it breaks one of `LINE_RULES`."""
    for rule in LINE_RULES:
        if rule.hits(line):
            return f"[hidden: {rule.name}]"
    return line


class CleanLines:
    """A text stream that writes each complete line to `out` through `clean`.

    The read commands (`ls`, `why`, `test`, `doctor`) print to this, so a value that a print site forgot
    to pass through `shown` still cannot put an escape sequence, a non-ASCII character or a secret on
    the terminal or into the agent that ran the command. It is the boundary; `shown` is the finer filter.

    A partial line stays buffered across `flush`, because a secret split over two writes would pass the
    scan in pieces. It is checked and printed only when its newline arrives or the command `close`s the
    stream, which leaves `out` itself open.
    """

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self._partial = ""

    def write(self, text: str) -> int:
        *lines, self._partial = (self._partial + text).split("\n")
        for line in lines:
            self.out.write(clean(line) + "\n")
        return len(text)

    def flush(self) -> None:
        self.out.flush()

    def close(self) -> None:
        if self._partial:
            self.out.write(clean(self._partial))
            self._partial = ""
        self.out.flush()

    def isatty(self) -> bool:
        return _is_tty(self.out)

    def fileno(self) -> int:
        return self.out.fileno()


def shown(value: str) -> str:
    """`value`, or `[hidden: <rule>]` when it breaks a lesson text rule.

    Agents run the read commands too, so whatever they print is a way into an agent's context. Stored
    values (lesson text, trigger specs, mined inputs, observations, event payloads) came from transcripts
    and are held to the same rules as lesson text before they are printed. An empty value breaks the
    sanitizer's empty rule too; a call site where empty is normal says so in words instead. The value of
    an option or key named as a secret (`--token X`, `DB_PASSWORD=X`) is masked, for rows stored before
    the miner refused them.
    """
    value = render.mask_secrets(value)
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
