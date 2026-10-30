"""The hook's best-effort state in telemetry.db: the cwd-to-repo cache, context epochs, delivered lessons
and hit rows.

The hook never creates telemetry.db or its directory; `scars init` does (through `store.init_telemetry`).
When the file is missing, unreadable, read-only, locked or broken, telemetry is unavailable: the read
returns the empty state (no cache, epoch 0, no dedupe) and the write returns False. Nothing here waits on
a lock: the connection runs with `busy_timeout = 0`. A telemetry problem changes neither the hook's answer
nor its latency, at the cost of one duplicate line or one lost row.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from scars.model import HitOutcome, LessonId, SessionId

CWD_CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class CwdRepo:
    cwd: str
    common_dir: str | None
    """`git rev-parse --git-common-dir` for this cwd, or None outside a git repo."""
    repo_id: str | None
    checked_at: float


@dataclass(frozen=True)
class State:
    cwd_repo: CwdRepo | None
    epoch: int
    shown: frozenset[tuple[LessonId, str]]
    """`(lesson_id, trigger_key)` pairs already delivered in this session and epoch."""


EMPTY = State(None, 0, frozenset())


@dataclass(frozen=True)
class Hit:
    """One `hits` row. Every column but `seq` is here; empty strings are stored as NULL."""

    at: float
    harness: str
    harness_version: str | None
    model_id: str | None
    session_id: str
    tool_use_id: str | None
    event: str
    stage: str | None
    lesson_id: str | None
    lesson_revision: int | None
    status_at_injection: str | None
    trigger_key: str | None
    outcome: HitOutcome
    injected_chars: int | None
    latency_ms: float | None
    error_class: str | None
    arm: str | None
    """The eval arm from `SCARS_ARM`, or None outside an eval."""


@dataclass(frozen=True)
class Writes:
    hits: tuple[Hit, ...]
    shown: tuple[tuple[LessonId, str], ...] = ()
    session_id: SessionId | None = None
    epoch: int = 0
    bump_epoch: bool = False
    """Store `epoch` as the session's context epoch: the event was a compaction."""
    cwd_repo: CwdRepo | None = None
    """A freshly resolved cache row to store, or None when the cache was used."""


def uri(path: str, mode: str) -> str:
    """A SQLite URI for `path`. SQLite ends the path at `?` or `#` and decodes `%HH`; nothing else needs
    escaping, which spares the hook importing `urllib.parse`."""
    escaped = path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    return f"file:{escaped}?mode={mode}"


def connect(path: str) -> sqlite3.Connection | None:
    """Open an existing telemetry.db for reading and writing, or None when it is unavailable."""
    try:
        db = sqlite3.connect(uri(path, "rw"), uri=True, isolation_level=None, timeout=0)
    except (sqlite3.Error, OSError):
        return None
    try:
        db.execute("pragma busy_timeout = 0")
        db.execute("pragma synchronous = NORMAL")
    except (sqlite3.Error, OSError):
        db.close()
        return None
    return db


def read(db: sqlite3.Connection | None, cwd: str, session_id: SessionId, *, compaction: bool) -> State:
    """The cache row, the session's epoch (one more on a compaction) and that epoch's shown pairs."""
    if db is None:
        return EMPTY
    try:
        row = db.execute(
            "select cwd, common_dir, repo_id, checked_at from cwd_repo where cwd = ?", (cwd,)
        ).fetchone()
        found = db.execute(
            "select context_epoch from session_epoch where session_id = ?", (session_id,)
        ).fetchone()
        epoch = (found[0] if found else 0) + (1 if compaction else 0)
        shown = db.execute(
            "select lesson_id, trigger_key from shown where session_id = ? and context_epoch = ?",
            (session_id, epoch),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return EMPTY
    cached = CwdRepo(*row) if row else None
    return State(cached, epoch, frozenset((LessonId(lid), key) for lid, key in shown))


def write(db: sqlite3.Connection | None, writes: Writes) -> bool:
    """Apply every write in one transaction, or none of them. False when the file is busy or broken."""
    if db is None:
        return False
    try:
        db.execute("begin immediate")
    except (sqlite3.Error, OSError):
        return False
    try:
        _insert_hits(db, writes.hits)
        if writes.session_id is not None:
            db.executemany(
                "insert or ignore into shown (session_id, context_epoch, lesson_id, trigger_key) "
                "values (?,?,?,?)",
                [(writes.session_id, writes.epoch, lid, key) for lid, key in writes.shown],
            )
            if writes.bump_epoch:
                db.execute(
                    "insert into session_epoch (session_id, context_epoch) values (?, ?) "
                    "on conflict(session_id) do update set "
                    "context_epoch = max(context_epoch, excluded.context_epoch)",
                    (writes.session_id, writes.epoch),
                )
        if (c := writes.cwd_repo) is not None:
            db.execute(
                "insert or replace into cwd_repo (cwd, repo_id, common_dir, checked_at) values (?,?,?,?)",
                (c.cwd, c.repo_id, c.common_dir, c.checked_at),
            )
        db.execute("commit")
        return True
    except (sqlite3.Error, OSError):
        _rollback(db)
        return False


def _rollback(db: sqlite3.Connection) -> None:
    # Plain try/except rather than contextlib.suppress: contextlib would cost the hook an import.
    try:
        db.execute("rollback")
    except (sqlite3.Error, OSError):
        return


def _insert_hits(db: sqlite3.Connection, hits: Iterable[Hit]) -> None:
    db.executemany(
        "insert into hits (at, harness, harness_version, model_id, session_id, tool_use_id, event, stage, "
        "lesson_id, lesson_revision, status_at_injection, trigger_key, outcome, injected_chars, latency_ms, "
        "error_class, arm) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                h.at,
                h.harness,
                h.harness_version or None,
                h.model_id or None,
                h.session_id,
                h.tool_use_id,
                h.event,
                h.stage,
                h.lesson_id,
                h.lesson_revision,
                h.status_at_injection,
                h.trigger_key,
                str(h.outcome),
                h.injected_chars,
                h.latency_ms,
                h.error_class,
                h.arm or None,
            )
            for h in hits
        ],
    )
