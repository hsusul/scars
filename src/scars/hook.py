"""The hook pipeline behind `scars-hook <harness> <event>` and `scars hook <harness> <event>`.

The hook fails open. Every path returns exit code 0, stderr stays empty, and stdout carries either nothing
or one valid JSON object. It never waits on a lock or a pipe for long, and it never emits `allow`, `ask`,
`updatedInput`, `continue` or `decision`.

One run, in order:

1. The kill switch (`~/.scars/OFF` or `SCARS_OFF`) skips all work. The `scars-hook` entry checks it before
   importing this module.
2. Stdin is read from the file descriptor: at most 1 MiB, within 500 ms in total. The payload is used as
   soon as it is one complete JSON object, so a harness that keeps the pipe open still gets an answer. A
   payload that is incomplete at the deadline, oversized or not a JSON object prints nothing. The harness
   adapter then parses it; malformed or ignored input prints nothing.
3. telemetry.db is opened if it already exists, never created, with no lock waits. It gives the
   cwd-to-repo cache, the session's context epoch (one more on a compaction) and the lessons already
   shown in that epoch. When it is unavailable the run has no cache, epoch 0, no dedupe and no writes.
4. On PreToolUse only the agent deny rule runs (see `agent_deny_reason`). Phase 1 has no block lessons, so
   gate-stage lessons are never read and nothing but that rule ever denies.
5. Otherwise the repo is resolved from the git common dir (cached for 24 hours), lessons.db is opened
   read-only with a 200 ms busy timeout, and only the lessons that can fire at this stage are loaded,
   filtered in SQL by stage, pin and trigger kind (and by exact error signature for repair). Matching and
   rendering are imported only when some lesson is left. Matching runs under a 50 ms deadline; lessons
   are rendered with their own observed passing commands and fitted to the stage budget.
6. The output is printed and flushed.
7. One telemetry transaction writes a hit row per shown lesson (or one `no_match` or `blocked` row), the
   new shown rows, a freshly resolved cache row and, on compaction, the new epoch. If the file is busy,
   all of it is dropped.

Any exception prints nothing, records an `error` hit when telemetry is available, and exits 0. The
traceback goes to `~/.scars/logs/hook.log` only when `SCARS_DEBUG` is set. `latency_ms` counts from the
start of the entry point, after the interpreter has started.
"""

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import TextIO

from scars import paths, telemetry
from scars.adapters import Ignored, MalformedInput, claude, codex, context_output, deny_output, full_command
from scars.model import (
    PRIVILEGED_COMMANDS,
    Harness,
    HitOutcome,
    HookEvent,
    HookEventKind,
    MachineScope,
    Stage,
    ToolKind,
    TriggerKind,
)

MATCH_DEADLINE_MS = 50
MATCH_CLOCK = time.monotonic
LESSONS_BUSY_TIMEOUT_MS = 200
GIT_TIMEOUT_SECONDS = 1.0
STDIN_LIMIT_BYTES = 1 << 20
STDIN_DEADLINE_MS = 500

ADAPTERS: dict[Harness, ModuleType] = {Harness.CLAUDE: claude, Harness.CODEX: codex}
AGENT_DENIED_SUBCOMMANDS = PRIVILEGED_COMMANDS | {"inbox"}
AGENT_DENY_KEY = "agent_deny"


@dataclass(frozen=True)
class StageQuery:
    """The SQL pre-filter for one event: what `match.STAGE_RULES` requires that SQL can check cheaply."""

    stage: Stage
    pinned_only: bool
    trigger_kinds: frozenset[TriggerKind] | None
    """Kinds that can fire. None means the stage selects without a trigger."""


_PLAN_START = StageQuery(Stage.PLAN, True, None)
QUERIES: dict[HookEventKind, StageQuery] = {
    HookEventKind.SESSION_START: _PLAN_START,
    HookEventKind.COMPACT: _PLAN_START,
    HookEventKind.PROMPT: StageQuery(Stage.PLAN, True, frozenset({TriggerKind.PROMPT, TriggerKind.PATH})),
    HookEventKind.TOOL_FAILED: StageQuery(Stage.REPAIR, False, frozenset({TriggerKind.ERROR})),
}
STAGES: dict[HookEventKind, Stage] = {
    **{kind: q.stage for kind, q in QUERIES.items()},
    HookEventKind.PRE_TOOL: Stage.GATE,
}


class LessonsUnavailable(Exception):
    """lessons.db is missing, unreadable, locked past its busy timeout, or not a database."""


class BadStdin(Exception):
    """Stdin did not deliver one JSON object in time or within the size limit."""

    def __init__(self, error_class: str):
        super().__init__(error_class)
        self.error_class = error_class


# ---------------------------------------------------------------- agent deny rule

RUN_REASON = (
    "scars: agents may not run `scars {}`. It changes which lessons reach agents, so a human runs it at a "
    "terminal."
)
SHELL_HOME_REASON = (
    "scars: this command names the scars home directory and is not a plain read (cat, ls, grep and the "
    "like, with no `>`). Lessons change only through scars commands a human runs at a terminal."
)
EDIT_REASON = (
    "scars: agents may not edit files under the scars home directory. Lessons change only through scars "
    "commands a human runs at a terminal."
)
READ_ONLY_PROGRAMS = frozenset(
    {"cat", "ls", "head", "tail", "less", "grep", "rg", "stat", "wc", "file", "find"}
)
FIND_ACTIONS_THAT_WRITE = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
_QUOTING = re.compile(r"[\"'\\]")
_invocation: re.Pattern[str] | None = None


def _invocation_pattern() -> re.Pattern[str]:
    """`scars <protected>` anywhere in the text, with or without a path before `scars`."""
    global _invocation
    if _invocation is None:
        names = "|".join(sorted(AGENT_DENIED_SUBCOMMANDS))
        _invocation = re.compile(rf"(?<![\w.-])scars\s+({names})(?![\w-])", re.IGNORECASE)
    return _invocation


def _home_pattern(home: str) -> re.Pattern[str]:
    roots = {home, os.path.realpath(home)}
    spelled = [r"(?:~|\$HOME|\$\{HOME\})/\.scars", r"\$SCARS_HOME(?!\w)", r"\$\{SCARS_HOME\}"]
    spelled += [re.escape(r.rstrip("/")) for r in sorted(roots) if r.rstrip("/")]
    return re.compile(rf"(?:{'|'.join(spelled)})(?![\w.-])", re.IGNORECASE)


def agent_deny_reason(
    event: HookEvent, home: str | os.PathLike[str], *, command: str | None = None
) -> str | None:
    """Why this PreToolUse call is denied, or None to stay silent.

    `command` is the full shell command before the 4 KB cut; it defaults to `event.command`. A shell call
    is denied when either holds:

    - The text runs a protected scars command: the word `scars` (bare, after a path such as
      `/usr/local/bin/scars`, or after `python -m` or `uv run`) followed by whitespace and `inbox` or a
      privileged command. One fixed regex scans the raw text, so a call inside a nested quoted script
      counts, and scans it again with quotes and backslashes removed, so `'sc''ars' off` counts too. The
      same scan also denies quoted text that merely reads like such a call, such as a commit message.
    - The text names the scars home (`~/.scars`, `$HOME/.scars`, `${HOME}/.scars`, `$SCARS_HOME`,
      `${SCARS_HOME}`, or the absolute home path) and the command is not provably read-only.

    Everything else fails open: a command the shell parser cannot model gets no deny just for mentioning
    the word scars. Edit tools are denied for any path under the scars home.
    """
    if event.event is not HookEventKind.PRE_TOOL:
        return None
    home = os.fspath(home)
    if event.tool_kind is ToolKind.SHELL:
        text = command if command is not None else event.command
        if text is None:
            return None
        unquoted = _QUOTING.sub("", text)
        for candidate in (text, unquoted):
            if found := _invocation_pattern().search(candidate):
                return RUN_REASON.format(found.group(1).lower())
        names_home = _home_pattern(home)
        if any(names_home.search(c) for c in (text, unquoted)) and not provably_read_only(text):
            return SHELL_HOME_REASON
    if event.tool_kind is ToolKind.EDIT:
        for path in event.paths:
            if _under(path, event.cwd, home):
                return EDIT_REASON
    return None


def provably_read_only(command: str) -> bool:
    """True only when the command parses and every program it runs just reads files.

    Every simple command must run a program from `READ_ONLY_PROGRAMS`, with no `>` outside quotes. `find`
    may not use an action that deletes, runs or writes, and `rg` may not use `--pre`, which runs a
    program. For those two an argument the shell expands into a word that starts with `$`, `*`, `?` or `[`
    could become such an option, so it is not read-only either.
    """
    if _redirects_output(command):
        return False
    from scars import shell

    try:
        commands = shell.parse(command)
    except shell.Unsupported:
        return False
    for c in commands:
        if c.program not in READ_ONLY_PROGRAMS:
            return False
        if c.program in ("find", "rg"):
            if any(c.args[i][:1] in ("$", "*", "?", "[") for i in c.uncertain):
                return False
            if c.program == "find" and FIND_ACTIONS_THAT_WRITE.intersection(c.args):
                return False
            if c.program == "rg" and any(a.startswith("--pre") for a in c.args):
                return False
    return True


def _redirects_output(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
        elif quote == "'":
            quote = None if ch == "'" else quote
        elif ch == "\\":
            escaped = True
        elif quote == '"':
            quote = None if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ">":
            return True
    return False


def _under(path: str, cwd: str, home: str) -> bool:
    full = os.path.join(cwd, os.path.expanduser(path))
    for candidate, root in (
        (os.path.abspath(full), os.path.abspath(home)),
        (os.path.realpath(full), os.path.realpath(home)),
    ):
        if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------- repo resolution


def git_common_dir(cwd: str) -> str | None:
    """`git rev-parse --git-common-dir` as an absolute path, or None outside a repo or without git."""
    import subprocess  # Only a cache miss needs it; importing it costs about 2 ms of the hook budget.

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def repo_root(cwd: str) -> str | None:
    """The symlink-resolved worktree root: the nearest directory at or above `cwd` holding `.git`."""
    if not os.path.isabs(cwd):
        return None
    here = os.path.realpath(cwd)
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _resolved(event: HookEvent) -> HookEvent:
    """Paths and cwd with symlinks resolved, because path globs match against the resolved repo root."""
    if not os.path.isabs(event.cwd):
        return event
    cwd = os.path.realpath(event.cwd)
    return replace(event, cwd=cwd, paths=tuple(os.path.realpath(os.path.join(cwd, p)) for p in event.paths))


def _open_lessons() -> sqlite3.Connection:
    """lessons.db read-only, with its schema already readable.

    A WAL database in a directory the hook cannot write cannot be opened normally, because SQLite has to
    create the `-shm` file. When the directory is not writable and there is no `-wal` file, no writer can
    have changes pending either, so the file is opened as immutable instead.
    """
    path = os.path.abspath(os.path.join(paths.home_path(), "lessons.db"))
    try:
        return _connect_ro(telemetry.uri(path, "ro"))
    except (sqlite3.Error, OSError) as exc:
        directory = os.path.dirname(path)
        if os.path.exists(path) and not os.access(directory, os.W_OK) and not os.path.exists(path + "-wal"):
            try:
                return _connect_ro(telemetry.uri(path, "ro") + "&immutable=1")
            except (sqlite3.Error, OSError):
                pass
        raise LessonsUnavailable(str(exc)) from exc


def _connect_ro(uri: str) -> sqlite3.Connection:
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        db.execute(f"pragma busy_timeout = {LESSONS_BUSY_TIMEOUT_MS}")
        db.execute("select 1 from lessons limit 0")
    except BaseException:
        db.close()
        raise
    return db


# ---------------------------------------------------------------- decision


@dataclass(frozen=True)
class Shown:
    lesson_id: str
    lesson_revision: int
    status: str
    trigger_key: str
    chars: int


@dataclass
class Decision:
    output: dict[str, object] | None = None
    shown: list[Shown] = field(default_factory=list)
    outcome: HitOutcome = HitOutcome.SHOWN
    trigger_key: str | None = None
    error_class: str | None = None
    cwd_repo: telemetry.CwdRepo | None = None


def decide(
    event: HookEvent,
    hook_event_name: str,
    state: telemetry.State,
    *,
    now: float,
    command: str | None = None,
) -> Decision:
    """Everything between reading telemetry and printing: what to output and what to record."""
    if event.event is HookEventKind.PRE_TOOL:
        if (reason := agent_deny_reason(event, paths.home_path(), command=command)) is not None:
            return Decision(deny_output(reason), outcome=HitOutcome.BLOCKED, trigger_key=AGENT_DENY_KEY)
        return Decision(outcome=HitOutcome.NO_MATCH)

    cached = state.cwd_repo
    fresh = None
    if cached is not None and now - cached.checked_at < telemetry.CWD_CACHE_SECONDS:
        common_dir = cached.common_dir
    else:
        common_dir = git_common_dir(event.cwd)
        fresh = telemetry.CwdRepo(event.cwd, common_dir, None, now)

    db = _open_lessons()
    try:
        return _decide_with(db, _resolved(event), hook_event_name, state, common_dir, fresh)
    except sqlite3.Error as exc:
        raise LessonsUnavailable(str(exc)) from exc
    finally:
        db.close()


def _decide_with(
    db: sqlite3.Connection,
    event: HookEvent,
    hook_event_name: str,
    state: telemetry.State,
    common_dir: str | None,
    fresh: telemetry.CwdRepo | None,
) -> Decision:
    from scars import store

    repo_id = (
        store.repo_for_common_dir(db, (common_dir, os.path.realpath(common_dir))) if common_dir else None
    )
    if fresh is not None:
        fresh = replace(fresh, repo_id=repo_id)
    decision = Decision(cwd_repo=fresh, outcome=HitOutcome.NO_MATCH)
    query = QUERIES[event.event]
    error_sig = None
    if event.event is HookEventKind.TOOL_FAILED and event.error_text:
        from scars.signature import signature

        error_sig = signature(event.error_text)
    if event.event is HookEventKind.TOOL_FAILED and error_sig is None:
        lessons = []
    else:
        lessons = store.active_for_stage(
            db,
            repo_id,
            event.session_id,
            stage=query.stage,
            pinned_only=query.pinned_only,
            trigger_kinds=query.trigger_kinds,
            error_signature=error_sig,
        )
    notice = None
    if event.event is HookEventKind.SESSION_START:
        waiting = store.candidate_count(db)
        if waiting:
            notice = f"{waiting} scars lesson{'s' if waiting != 1 else ''} waiting: run scars inbox"
    if not lessons:
        decision.output = context_output(hook_event_name, None, notice)
        return decision

    from datetime import date

    from scars import match, render

    context = match.MatchContext(repo_root=repo_root(event.cwd), repo_id=repo_id, error_signature=error_sig)
    selection = match.select(
        lessons,
        event,
        context,
        scores=None,
        shown=state.shown,
        deadline_ms=MATCH_DEADLINE_MS,
        clock=MATCH_CLOCK,
    )
    if selection.deadline_hit:
        decision.error_class = "match_deadline"
    ids = [s.lesson.id for s in selection.selected]
    known = store.passing_inputs(db, ids)
    names = (
        store.repo_names(db)
        if any(isinstance(s.lesson.scope, MachineScope) for s in selection.selected)
        else ()
    )
    reviewed = store.promoted_at(db, ids)
    by_id = {s.lesson.id: s for s in selection.selected}
    rendered: list[render.Rendered] = []
    for s in selection.selected:
        at = reviewed.get(s.lesson.id)
        try:
            text = render.render(
                s.lesson,
                known_commands=known[s.lesson.id],
                reviewed_on=date.fromtimestamp(at) if at is not None else None,
                registered_repos=names,
            )
        except render.RejectedText:
            decision.error_class = decision.error_class or "render_rejected"
            continue
        rendered.append(render.Rendered(s.lesson.id, text))
    budget = render.REPAIR_BUDGET
    if event.event is HookEventKind.PROMPT:
        budget = render.PROMPT_BUDGET
    elif event.event in (HookEventKind.SESSION_START, HookEventKind.COMPACT):
        budget = render.SESSION_START_BUDGET
    fitted = render.fit(rendered, budget)
    for r in fitted.kept:
        s = by_id[r.lesson_id]
        decision.shown.append(
            Shown(s.lesson.id, s.lesson.revision, s.lesson.status.value, s.trigger_key, len(r.text))
        )
    decision.output = context_output(hook_event_name, fitted.text, notice)
    if decision.shown:
        decision.outcome = HitOutcome.SHOWN
    return decision


# ---------------------------------------------------------------- telemetry rows


def _hit(
    harness: Harness,
    event: HookEvent | None,
    outcome: HitOutcome,
    *,
    now: float,
    latency_ms: float,
    shown: Shown | None = None,
    trigger_key: str | None = None,
    error_class: str | None = None,
) -> telemetry.Hit:
    return telemetry.Hit(
        at=now,
        harness=harness.value,
        harness_version=(event.harness_version or None) if event else None,
        model_id=(event.model_id or None) if event else None,
        session_id=event.session_id if event else "",
        tool_use_id=event.tool_use_id if event else None,
        event=event.event.value if event else "unknown",
        stage=STAGES[event.event].value if event else None,
        lesson_id=shown.lesson_id if shown else None,
        lesson_revision=shown.lesson_revision if shown else None,
        status_at_injection=shown.status if shown else None,
        trigger_key=shown.trigger_key if shown else trigger_key,
        outcome=outcome,
        injected_chars=shown.chars if shown else 0,
        latency_ms=latency_ms,
        error_class=error_class,
        arm=os.environ.get("SCARS_ARM") or None,
    )


def _hits(
    event: HookEvent, decision: Decision, *, now: float, latency_ms: float
) -> tuple[telemetry.Hit, ...]:
    def hit(shown: Shown | None) -> telemetry.Hit:
        return _hit(
            event.harness,
            event,
            decision.outcome,
            now=now,
            latency_ms=latency_ms,
            shown=shown,
            trigger_key=decision.trigger_key,
            error_class=decision.error_class,
        )

    return tuple(hit(s) for s in decision.shown) if decision.shown else (hit(None),)


def _telemetry_path() -> str:
    return os.path.join(paths.home_path(), "telemetry.db")


# ---------------------------------------------------------------- stdin


def _parse_payload(data: bytes | str) -> dict[str, object]:
    try:
        payload = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise MalformedInput("stdin is not JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedInput("stdin is not a JSON object")
    return payload


def read_payload_fd(
    fd: int, *, limit: int = STDIN_LIMIT_BYTES, deadline_ms: int = STDIN_DEADLINE_MS
) -> dict[str, object]:
    """One JSON object from `fd`, read without ever blocking past the deadline.

    `select` waits for data with the time that is left, and each `os.read` takes only what is already
    there. The payload is parsed whenever the bytes so far end in `}`, so a complete object is answered
    even if the writer never closes the pipe. Raises `BadStdin` on timeout or overflow and
    `MalformedInput` when the bytes at end of input are not one JSON object.
    """
    import select

    end = time.monotonic() + deadline_ms / 1000
    data = b""
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise BadStdin("stdin_timeout")
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return _parse_payload(data)
            data += chunk
            if len(data) > limit:
                raise BadStdin("stdin_oversized")
            if data.rstrip().endswith(b"}"):
                try:
                    return _parse_payload(data)
                except MalformedInput:
                    continue
    except OSError as exc:
        raise BadStdin("stdin_unreadable") from exc


def read_payload(stdin: TextIO) -> dict[str, object]:
    try:
        fd = stdin.fileno()
    except (OSError, ValueError):
        text = stdin.read(STDIN_LIMIT_BYTES + 1)
        if len(text.encode()) > STDIN_LIMIT_BYTES:
            raise BadStdin("stdin_oversized") from None
        return _parse_payload(text)
    return read_payload_fd(fd)


# ---------------------------------------------------------------- entry


def run(
    harness: Harness, event_name: str, stdin: TextIO, stdout: TextIO, *, started: float | None = None
) -> None:
    """One hook run. Raises on any fault; `main` contains it."""
    started = time.perf_counter() if started is None else started

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    tele = None
    try:
        try:
            payload = read_payload(stdin)
            event = ADAPTERS[harness].parse(event_name, payload)
        except Ignored:
            return
        except (MalformedInput, BadStdin) as exc:
            error_class = exc.error_class if isinstance(exc, BadStdin) else "malformed_input"
            tele = telemetry.connect(_telemetry_path())
            hit = _hit(
                harness,
                None,
                HitOutcome.ERROR,
                now=time.time(),
                latency_ms=elapsed_ms(),
                error_class=error_class,
            )
            telemetry.write(tele, telemetry.Writes((hit,)))
            raise
        tele = telemetry.connect(_telemetry_path())
        compaction = event.event is HookEventKind.COMPACT
        state = (
            telemetry.EMPTY
            if event.event is HookEventKind.PRE_TOOL
            else telemetry.read(tele, event.cwd, event.session_id, compaction=compaction)
        )
        now = time.time()
        command = full_command(payload) if event.tool_kind is ToolKind.SHELL else None
        try:
            decision = decide(event, event_name, state, now=now, command=command)
        except Exception as exc:
            error_class = "lessons_unavailable" if isinstance(exc, LessonsUnavailable) else type(exc).__name__
            hit = _hit(
                harness, event, HitOutcome.ERROR, now=now, latency_ms=elapsed_ms(), error_class=error_class
            )
            telemetry.write(tele, telemetry.Writes((hit,)))
            raise
        if decision.output is not None:
            stdout.write(json.dumps(decision.output))
            stdout.flush()
        dedupe = decision.outcome is HitOutcome.SHOWN
        telemetry.write(
            tele,
            telemetry.Writes(
                hits=_hits(event, decision, now=now, latency_ms=elapsed_ms()),
                shown=tuple((s.lesson_id, s.trigger_key) for s in decision.shown) if dedupe else (),
                session_id=event.session_id,
                epoch=state.epoch,
                bump_epoch=compaction,
                cwd_repo=decision.cwd_repo,
            ),
        )
    finally:
        if tele is not None:
            tele.close()


def main(harness: str, event_name: str, *, started: float | None = None) -> int:
    if paths.is_off():
        return 0
    started = time.perf_counter() if started is None else started
    try:
        run(Harness(harness), event_name, sys.stdin, sys.stdout, started=started)
    except Exception:
        if os.environ.get("SCARS_DEBUG"):
            import traceback

            try:
                logs = os.path.join(paths.home_path(), "logs")
                os.makedirs(logs, exist_ok=True)
                with open(os.path.join(logs, "hook.log"), "a") as f:
                    f.write(traceback.format_exc())
            except OSError:
                pass
    return 0
