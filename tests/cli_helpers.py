"""Shared setup for the CLI tests: a fake terminal, a temporary scars home and seeded lessons."""

import io
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from scars import cli, render, store
from scars.commands import Context
from scars.model import (
    EventType,
    LessonId,
    LessonText,
    RepoId,
    RepoScope,
    Stage,
    Trigger,
    TriggerKind,
    TrustClass,
)

NOW = 1_800_000_000.0
"""2027-01-15 08:00 UTC; the tests run with TZ=UTC."""
DAY = 86400.0
REPO = RepoId("app")
SIG = "pytest: error: unrecognized arguments: --frobnicate"


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def make_ctx(
    user_home: Path,
    *,
    keys: Iterable[str] = (),
    lines: str = "",
    tty: bool = True,
    env: dict[str, str] | None = None,
    ancestors: Iterable[str] = ("-zsh", "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
    edit: Callable[[str], str] | None = None,
    clock: Callable[[], float] = lambda: NOW,
    platform: str = "darwin",
) -> Context:
    stream = Tty if tty else io.StringIO
    pressed = iter(keys)
    return Context(
        stdin=stream(lines),
        stdout=stream(),
        stderr=io.StringIO(),
        env=env or {},
        read_key=lambda: next(pressed, ""),
        ancestors=lambda: list(ancestors),
        edit=edit,
        clock=clock,
        user_home=user_home,
        platform=platform,
    )


def run(ctx: Context, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv), ctx)
    return code, ctx.stdout.getvalue(), ctx.stderr.getvalue()  # type: ignore[attr-defined]


def init_home(home: Path) -> None:
    db = store.init_lessons(home / "lessons.db")
    store.ensure_miner_schema(db)
    store.add_repo(db, REPO, "app", "own")
    store.add_clone(db, "/git/app", REPO, NOW - 10 * DAY)
    db.close()
    store.init_telemetry(home / "telemetry.db").close()


def connect(home: Path) -> sqlite3.Connection:
    db = store.connect(home / "lessons.db")
    db.row_factory = sqlite3.Row
    return db


def seed(
    home: Path,
    *,
    avoid: str = "pytest --frobnicate",
    passing: str = "pytest tests",
    sig: str = SIG,
    provisional: bool = True,
    pre_trigger: str | None = "pytest ** --frobnicate",
    at: float = NOW - DAY,
    trust: TrustClass = TrustClass.OWN_CODE,
    reason: str = render.MINED_REASON,
) -> LessonId:
    """One mined-looking repo lesson with one observation, provisional unless told otherwise."""
    db = connect(home)
    triggers = [Trigger(TriggerKind.ERROR, sig, Stage.REPAIR)]
    if pre_trigger:
        triggers.append(Trigger(TriggerKind.COMMAND, pre_trigger, Stage.GATE))
    lesson_id = store.create_lesson(
        db,
        text=LessonText(avoid, f"run `{passing}` instead", reason),
        scope=RepoScope(REPO),
        trust_class=trust,
        triggers=triggers,
        fingerprint=f"fp-{avoid}-{sig}",
        actor="miner",
        at=at,
    )
    store.add_observation(
        db,
        store.Observation(lesson_id, "claude", "s1", f"tu-{lesson_id}", f"{avoid} tests", sig, passing, at),
    )
    if provisional:
        store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PROVISIONALIZED,
            expected_revision=1,
            actor="miner",
            payload={},
            at=at,
        )
    db.close()
    return lesson_id


def human(
    home: Path, lesson_id: LessonId, event: EventType, payload: dict[str, object] | None = None
) -> None:
    db = connect(home)
    lesson = store.get_lesson(db, lesson_id)
    assert lesson is not None
    store.apply_event(
        db,
        lesson_id=lesson_id,
        event=event,
        expected_revision=lesson.revision,
        actor="human",
        payload=payload or {},
        at=NOW - 3600,
    )
    db.close()


def iso(t: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def claude_transcript(cwd: str, start: float, calls: list[tuple[str, str, str | None]]) -> str:
    """Claude Code transcript lines for Bash calls `(tool_use_id, command, error or None)`."""
    import json

    records: list[object] = []
    at = start
    for tool_id, command, error in calls:
        at += 10
        records.append(
            {
                "type": "assistant",
                "timestamp": iso(at),
                "cwd": cwd,
                "message": {
                    "model": "claude-test-1",
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
                    ],
                },
            }
        )
        result = {"type": "tool_result", "tool_use_id": tool_id, "is_error": error is not None}
        result["content"] = error if error is not None else "fine"
        records.append(
            {"type": "user", "timestamp": iso(at + 1), "cwd": cwd, "message": {"content": [result]}}
        )
    return "".join(json.dumps(r) + "\n" for r in records)


FAILURE_PAIR = [
    ("t1", "pytest --frobnicate tests", SIG),
    ("t2", "pytest tests", None),
]


def git_repo(path: Path, remote: str | None = None) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path
