"""Entry point for `scars hook <harness> <event>`.

The hook fails open. Every path returns exit code 0, and stdout carries either nothing or one valid
JSON object. A fault in scars must never block or slow an agent's tool call.

One run, in order:

1. The kill switch (`~/.scars/OFF` or `SCARS_OFF`) skips all work.
2. Stdin is parsed by the harness adapter. Malformed or ignored input prints nothing.
3. telemetry.db is read without waiting: the cwd-to-repo cache, the session's context epoch (one more on
   a compaction) and the lessons already shown in that epoch. A busy or missing file means no cache,
   epoch 0 and no dedupe.
4. On PreToolUse, the agent deny rule runs before the store is touched (see `agent_deny_reason`).
5. The repo is resolved from the git common dir (cached for 24 hours), lessons.db is opened read-only
   with a 200 ms busy timeout, and the stage's lessons are selected under a 50 ms deadline, rendered
   with their own observed passing commands, and fitted to the stage budget.
6. The output is printed and flushed.
7. One telemetry transaction writes a hit row per shown lesson (or one `no_match` row), the new shown
   rows, a freshly resolved cache row and, on compaction, the new epoch. If the file is busy, all of it
   is dropped.

Any exception prints nothing, records an `error` hit when it can, and exits 0. Nothing reaches stderr;
the traceback goes to `~/.scars/logs/hook.log` only when `SCARS_DEBUG` is set. `latency_ms` counts
from the start of `main`, after the interpreter and imports have loaded.
"""

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import TextIO

from scars import match, paths, render, shell, store, telemetry
from scars.adapters import Ignored, MalformedInput, claude, codex, context_output, deny_output
from scars.model import (
    PRIVILEGED_COMMANDS,
    Harness,
    HitOutcome,
    HookEvent,
    HookEventKind,
    MachineScope,
    ToolKind,
)
from scars.signature import signature

MATCH_DEADLINE_MS = 50
LESSONS_BUSY_TIMEOUT_MS = 200
GIT_TIMEOUT_SECONDS = 1.0

ADAPTERS: dict[Harness, ModuleType] = {Harness.CLAUDE: claude, Harness.CODEX: codex}
BUDGETS = {
    HookEventKind.SESSION_START: render.SESSION_START_BUDGET,
    HookEventKind.COMPACT: render.SESSION_START_BUDGET,
    HookEventKind.PROMPT: render.PROMPT_BUDGET,
    HookEventKind.TOOL_FAILED: render.REPAIR_BUDGET,
}
AGENT_DENIED_SUBCOMMANDS = PRIVILEGED_COMMANDS | {"inbox"}
AGENT_DENY_KEY = "agent_deny"


class LessonsUnavailable(Exception):
    """lessons.db is missing, unreadable, locked past its busy timeout, or not a database."""


# ---------------------------------------------------------------- agent deny rule


def agent_deny_reason(event: HookEvent, home: Path) -> str | None:
    """Why this PreToolUse call is denied regardless of any lesson, or None to stay silent.

    Agents may not run `scars inbox` or a privileged command (as `scars <cmd>`, also through wrappers
    such as `uv run`, `python -m` or `bash -c`), and may not edit files under the scars home directory.

    This is the one place where a parse failure leads to a deny instead of no output: a shell command
    that the parser cannot model and that mentions `scars` is denied. Everywhere else an unsupported parse
    must stay silent, because a wrong deny stops real work. Here the target is scars's own control plane,
    so a wrong deny only stops an agent from changing scars, which an agent should not be doing anyway.
    """
    if event.event is not HookEventKind.PRE_TOOL:
        return None
    if event.tool_kind is ToolKind.SHELL and event.command is not None:
        try:
            commands = shell.parse(event.command)
        except shell.Unsupported:
            if "scars" in event.command.lower():
                return (
                    "scars: this command mentions scars but cannot be parsed, so scars cannot tell "
                    "whether it runs a command reserved for a human at a terminal. "
                    "Use a simpler form or ask the user."
                )
            return None
        for command in commands:
            if command.program == "scars" or command.program.startswith("scars."):
                denied = _denied_subcommand(command)
                if denied is not None:
                    return (
                        f"scars: agents may not run `scars {denied}`. It changes which lessons reach agents, "
                        "so a human runs it at a terminal."
                    )
    if event.tool_kind is ToolKind.EDIT:
        for path in event.paths:
            if _under(path, event.cwd, home):
                return (
                    "scars: agents may not edit files under the scars home directory. Lessons change only "
                    "through scars commands a human runs at a terminal."
                )
    return None


def _denied_subcommand(command: shell.SimpleCommand) -> str | None:
    """The denied subcommand, or `<unknown>` when the first operand is expanded at run time."""
    for i, arg in enumerate(command.args):
        if i in command.uncertain:
            return "<unknown>"
        if not arg.startswith("-"):
            return arg if arg in AGENT_DENIED_SUBCOMMANDS else None
    return None


def _under(path: str, cwd: str, home: Path) -> bool:
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


# ---------------------------------------------------------------- decision


@dataclass(frozen=True)
class Shown:
    selected: match.Selected
    chars: int


@dataclass
class Decision:
    output: dict[str, object] | None = None
    shown: list[Shown] = field(default_factory=list)
    outcome: HitOutcome = HitOutcome.SHOWN
    trigger_key: str | None = None
    error_class: str | None = None
    cwd_repo: telemetry.CwdRepo | None = None


def decide(event: HookEvent, hook_event_name: str, state: telemetry.State, *, now: float) -> Decision:
    """Everything between reading telemetry and printing: what to output and what to record."""
    if (reason := agent_deny_reason(event, paths.home())) is not None:
        return Decision(deny_output(reason), outcome=HitOutcome.BLOCKED, trigger_key=AGENT_DENY_KEY)

    cached = state.cwd_repo
    fresh = None
    if cached is not None and now - cached.checked_at < telemetry.CWD_CACHE_SECONDS:
        common_dir = cached.common_dir
    else:
        common_dir = git_common_dir(event.cwd)
        fresh = telemetry.CwdRepo(event.cwd, common_dir, None, now)

    try:
        db = store.connect(paths.lessons_db(), readonly=True, busy_timeout_ms=LESSONS_BUSY_TIMEOUT_MS)
    except sqlite3.Error as exc:
        raise LessonsUnavailable(str(exc)) from exc
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
    repo_id = (
        store.repo_for_common_dir(db, (common_dir, os.path.realpath(common_dir))) if common_dir else None
    )
    if fresh is not None:
        fresh = replace(fresh, repo_id=repo_id)
    error_sig = (
        signature(event.error_text) if event.event is HookEventKind.TOOL_FAILED and event.error_text else None
    )
    context = match.MatchContext(repo_root=repo_root(event.cwd), repo_id=repo_id, error_signature=error_sig)
    lessons = store.active_for_hook(db, repo_id, event.session_id)
    selection = match.select(
        lessons, event, context, scores=None, shown=state.shown, deadline_ms=MATCH_DEADLINE_MS
    )
    decision = Decision(cwd_repo=fresh, error_class="match_deadline" if selection.deadline_hit else None)
    if not selection.selected and event.event is not HookEventKind.SESSION_START:
        decision.outcome = HitOutcome.NO_MATCH
        return decision

    ids = [s.lesson.id for s in selection.selected]
    known = store.passing_inputs(db, ids)
    names = (
        store.repo_names(db)
        if any(isinstance(s.lesson.scope, MachineScope) for s in selection.selected)
        else ()
    )

    if event.event is HookEventKind.PRE_TOOL:
        for s in selection.selected:
            try:
                reason = render.render_deny(
                    s.lesson, known_commands=known[s.lesson.id], registered_repos=names
                )
            except render.RejectedText:
                continue
            decision.output = deny_output(reason)
            decision.outcome = HitOutcome.BLOCKED
            decision.shown = [Shown(s, len(reason))]
            return decision
        decision.outcome = HitOutcome.NO_MATCH
        return decision

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
    fitted = render.fit(rendered, BUDGETS[event.event])
    decision.shown = [Shown(by_id[r.lesson_id], len(r.text)) for r in fitted.kept]
    notice = None
    if event.event is HookEventKind.SESSION_START:
        waiting = store.candidate_count(db)
        if waiting:
            notice = f"{waiting} scars lesson{'s' if waiting != 1 else ''} waiting: run scars inbox"
    decision.output = context_output(hook_event_name, fitted.text, notice)
    if not decision.shown:
        decision.outcome = HitOutcome.NO_MATCH
    return decision


# ---------------------------------------------------------------- telemetry rows


def _hits(
    event: HookEvent, decision: Decision, *, now: float, latency_ms: float
) -> tuple[telemetry.Hit, ...]:
    stage = match.STAGE_RULES[event.event].stage.value

    def hit(outcome: HitOutcome, s: Shown | None, trigger_key: str | None) -> telemetry.Hit:
        lesson = s.selected.lesson if s else None
        return telemetry.Hit(
            at=now,
            harness=event.harness.value,
            harness_version=event.harness_version,
            model_id=event.model_id,
            session_id=event.session_id,
            tool_use_id=event.tool_use_id,
            event=event.event.value,
            stage=stage,
            lesson_id=lesson.id if lesson else None,
            lesson_revision=lesson.revision if lesson else None,
            status_at_injection=lesson.status.value if lesson else None,
            trigger_key=trigger_key,
            outcome=outcome,
            injected_chars=s.chars if s else 0,
            latency_ms=latency_ms,
            error_class=decision.error_class,
        )

    if decision.shown:
        return tuple(hit(decision.outcome, s, s.selected.trigger_key) for s in decision.shown)
    return (hit(decision.outcome, None, decision.trigger_key),)


def _error_hit(
    harness: Harness, event: HookEvent | None, error_class: str, *, now: float, latency_ms: float
) -> telemetry.Hit:
    return telemetry.Hit(
        at=now,
        harness=harness.value,
        harness_version=event.harness_version if event else None,
        model_id=event.model_id if event else None,
        session_id=event.session_id if event else "",
        tool_use_id=event.tool_use_id if event else None,
        event=event.event.value if event else "unknown",
        stage=match.STAGE_RULES[event.event].stage.value if event else None,
        lesson_id=None,
        lesson_revision=None,
        status_at_injection=None,
        trigger_key=None,
        outcome=HitOutcome.ERROR,
        injected_chars=0,
        latency_ms=latency_ms,
        error_class=error_class,
    )


# ---------------------------------------------------------------- entry


def _read_payload(stdin: TextIO) -> dict[str, object]:
    try:
        payload = json.loads(stdin.read())
    except ValueError as exc:
        raise MalformedInput("stdin is not JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedInput("stdin is not a JSON object")
    return payload


def run(
    harness: Harness, event_name: str, stdin: TextIO, stdout: TextIO, *, started: float | None = None
) -> None:
    """One hook run. Raises on any fault; `main` contains it."""
    started = time.perf_counter() if started is None else started

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    event: HookEvent | None = None
    tele = None
    try:
        try:
            event = ADAPTERS[harness].parse(event_name, _read_payload(stdin))
        except Ignored:
            return
        except MalformedInput:
            tele = telemetry.connect(paths.telemetry_db())
            hit = _error_hit(harness, None, "malformed_input", now=time.time(), latency_ms=elapsed_ms())
            telemetry.write(tele, telemetry.Writes((hit,)))
            raise
        tele = telemetry.connect(paths.telemetry_db())
        compaction = event.event is HookEventKind.COMPACT
        state = telemetry.read(tele, event.cwd, event.session_id, compaction=compaction)
        now = time.time()
        try:
            decision = decide(event, event_name, state, now=now)
        except Exception as exc:
            error_class = "lessons_unavailable" if isinstance(exc, LessonsUnavailable) else type(exc).__name__
            hit = _error_hit(harness, event, error_class, now=now, latency_ms=elapsed_ms())
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
                shown=tuple((s.selected.lesson.id, s.selected.trigger_key) for s in decision.shown)
                if dedupe
                else (),
                session_id=event.session_id,
                epoch=state.epoch,
                bump_epoch=compaction,
                cwd_repo=decision.cwd_repo,
            ),
        )
    finally:
        if tele is not None:
            tele.close()


def main(harness: str, event_name: str) -> int:
    if paths.is_off():
        return 0
    started = time.perf_counter()
    try:
        run(Harness(harness), event_name, sys.stdin, sys.stdout, started=started)
    except Exception:
        if os.environ.get("SCARS_DEBUG"):
            import traceback

            try:
                log = paths.home() / "logs" / "hook.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a") as f:
                    f.write(traceback.format_exc())
            except OSError:
                pass
    return 0
