"""The hook's best-effort state in telemetry.db: the cwd-to-repo cache, context epochs, delivered lessons
and hit rows.

Nothing here waits on a lock. Reads and the one write transaction run with `busy_timeout = 0`; a busy or
missing file makes the read return the empty state and the write return False. A telemetry problem
changes neither the hook's answer nor its latency, at the cost of one duplicate line or one lost row.
"""

import contextlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from scars import store
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


def connect(path: Path) -> sqlite3.Connection | None:
    """Open telemetry.db, creating it with its schema the first time. None when it cannot be opened."""
    try:
        if not path.exists():
            store.init_telemetry(path).close()
        uri = "file:" + quote(str(path.absolute())) + "?mode=rw"
        db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
        db.execute("pragma busy_timeout = 0")
        return db
    except sqlite3.Error:
        return None


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
    except sqlite3.Error:
        return EMPTY
    cached = CwdRepo(*row) if row else None
    return State(cached, epoch, frozenset((LessonId(lid), key) for lid, key in shown))


def write(db: sqlite3.Connection | None, writes: Writes) -> bool:
    """Apply every write in one transaction, or none of them. False when the file is busy or broken."""
    if db is None:
        return False
    try:
        db.execute("begin immediate")
    except sqlite3.Error:
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
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            db.execute("rollback")
        return False


def _insert_hits(db: sqlite3.Connection, hits: Iterable[Hit]) -> None:
    db.executemany(
        "insert into hits (at, harness, harness_version, model_id, session_id, tool_use_id, event, stage, "
        "lesson_id, lesson_revision, status_at_injection, trigger_key, outcome, injected_chars, latency_ms, "
        "error_class) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                h.at,
                h.harness,
                h.harness_version or None,
                h.model_id,
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
            )
            for h in hits
        ],
    )
