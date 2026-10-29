"""SQLite persistence for lessons.db and telemetry.db. See docs/adr/0002-sqlite-store.md."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from scars.model import (
    LIFECYCLE,
    EventType,
    Lesson,
    LessonId,
    LessonText,
    Level,
    MachineScope,
    PathScope,
    RepoId,
    RepoScope,
    Scope,
    SessionId,
    SessionScope,
    Stage,
    Status,
    Trigger,
    TriggerKind,
    TrustClass,
)

SCHEMA_VERSION = 1
EXPIRY_SECONDS = 14 * 86400
GENESIS_HASH = "0" * 64
BLOCK_MIN_FAILING = 3
BLOCK_MAX_PASSING_SHARE = 0.01
NO_BLOCK_TRUST = frozenset({TrustClass.THIRD_PARTY_REPO, TrustClass.WEB})
EXPIRING = frozenset({Status.CANDIDATE, Status.PROVISIONAL})


def schema_sql(name: str) -> str:
    """The DDL for `lessons` or `telemetry`, shipped in `scars/schema/`."""
    return files("scars.schema").joinpath(f"{name}.sql").read_text()


def connect(
    path: Path,
    *,
    readonly: bool = False,
    busy_timeout_ms: int = 2000,
    synchronous: str = "FULL",
) -> sqlite3.Connection:
    """Open a store file. Transactions are explicit: the connection runs in autocommit mode."""
    if readonly:
        uri = "file:" + quote(str(Path(path).absolute())) + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        if synchronous.upper() not in ("FULL", "NORMAL"):
            raise ValueError(f"synchronous must be FULL or NORMAL, got {synchronous}")
        db = sqlite3.connect(path, isolation_level=None)
    db.execute(f"pragma busy_timeout = {int(busy_timeout_ms)}")
    db.execute("pragma foreign_keys = on")
    if not readonly:
        db.execute("pragma journal_mode = wal")
        db.execute(f"pragma synchronous = {synchronous.upper()}")
    return db


def _statements(sql: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def _init(path: Path, schema: str, synchronous: str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    os.chmod(path, 0o600)
    db = connect(path, synchronous=synchronous)
    # The version check runs inside the write lock so two concurrent inits apply the DDL once.
    db.execute("begin immediate")
    try:
        if db.execute("pragma user_version").fetchone()[0] == 0:
            for statement in _statements(schema_sql(schema)):
                db.execute(statement)
            db.execute(f"pragma user_version = {SCHEMA_VERSION}")
        db.execute("commit")
    except BaseException:
        _rollback(db)
        db.close()
        raise
    return db


def init_lessons(path: Path) -> sqlite3.Connection:
    return _init(path, "lessons", "FULL")


def init_telemetry(path: Path) -> sqlite3.Connection:
    return _init(path, "telemetry", "NORMAL")


class Conflict(Exception):
    """A compare-and-set update found a newer revision than the caller expected."""


class Rejected(Exception):
    """The lifecycle table forbids this event for the lesson's current status."""


@dataclass(frozen=True)
class State:
    """The lesson fields the event log determines, and that `verify` recomputes."""

    status: Status
    level: Level
    pinned: bool
    scope_kind: str
    revision: int


def _parse_scope(data: object) -> Scope:
    if not isinstance(data, dict):
        raise Rejected(f"scope must be an object, got {data!r}")
    match data.get("kind"):
        case "machine":
            return MachineScope()
        case "repo" if isinstance(data.get("repo_id"), str):
            return RepoScope(RepoId(data["repo_id"]))
        case "path" if isinstance(data.get("repo_id"), str) and isinstance(data.get("glob"), str):
            return PathScope(RepoId(data["repo_id"]), data["glob"])
        case "session" if isinstance(data.get("session_id"), str):
            return SessionScope(SessionId(data["session_id"]))
    raise Rejected(f"invalid scope {data!r}")


def scope_to_json(scope: Scope) -> dict[str, str]:
    match scope:
        case MachineScope():
            return {"kind": "machine"}
        case RepoScope(repo_id=repo_id):
            return {"kind": "repo", "repo_id": repo_id}
        case PathScope(repo_id=repo_id, glob=glob):
            return {"kind": "path", "repo_id": repo_id, "glob": glob}
        case SessionScope(session_id=session_id):
            return {"kind": "session", "session_id": session_id}


def _scope_columns(scope: Scope) -> dict[str, str | None]:
    cols: dict[str, str | None] = {
        "scope_kind": scope.kind,
        "repo_id": None,
        "path_glob": None,
        "session_id": None,
    }
    match scope:
        case RepoScope(repo_id=repo_id):
            cols["repo_id"] = repo_id
        case PathScope(repo_id=repo_id, glob=glob):
            cols["repo_id"] = repo_id
            cols["path_glob"] = glob
        case SessionScope(session_id=session_id):
            cols["session_id"] = session_id
    return cols


def _scope_from_row(row: sqlite3.Row) -> Scope:
    match row["scope_kind"]:
        case "machine":
            return MachineScope()
        case "repo":
            return RepoScope(RepoId(row["repo_id"]))
        case "path":
            return PathScope(RepoId(row["repo_id"]), row["path_glob"])
        case "session":
            return SessionScope(SessionId(row["session_id"]))
    raise ValueError(f"unknown scope_kind {row['scope_kind']!r}")


def next_state(state: State | None, event: EventType, payload: dict[str, object]) -> State:
    """Apply one event to a lesson's state, or raise `Rejected`. Pure; shared by writes and `verify`."""
    rule = LIFECYCLE[event]
    if event is EventType.PROPOSED:
        if state is not None:
            raise Rejected("proposed applies only to a new lesson")
        kind = _parse_scope(payload.get("scope")).kind
        if kind == "machine":
            raise Rejected("a machine-scope lesson cannot be a candidate")
        return State(Status.CANDIDATE, Level.NOTE, False, kind, 1)
    if state is None:
        raise Rejected(f"{event} needs an existing lesson")
    if state.status not in rule.allowed_from:
        raise Rejected(f"{event} is not allowed from {state.status}")
    new = replace(state, status=rule.to or state.status, revision=state.revision + 1)
    match event:
        case EventType.PROMOTED if "scope" in payload:
            new = replace(new, scope_kind=_parse_scope(payload["scope"]).kind)
        case EventType.PINNED:
            new = replace(new, pinned=True)
        case EventType.UNPINNED:
            new = replace(new, pinned=False)
        case EventType.LEVEL_SET:
            try:
                new = replace(new, level=Level(payload.get("level")))
            except ValueError:
                raise Rejected(f"invalid level {payload.get('level')!r}") from None
        case EventType.EDITED:
            new = replace(new, pinned=False, level=Level.NOTE)
            if state.scope_kind == "machine":
                new = replace(new, status=Status.PROMOTED)
        case EventType.SUPERSEDED if not isinstance(payload.get("superseded_by"), str):
            raise Rejected("superseded needs superseded_by")
    if new.status is not Status.PROMOTED:
        new = replace(new, pinned=False, level=Level.NOTE)
    return new


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_hash(row: sqlite3.Row) -> str:
    return hashlib.sha256(_canonical(dict(row)).encode()).hexdigest()


def _rows(db: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, tuple(params)).fetchall()


def _rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("rollback")


def _append_event(
    db: sqlite3.Connection,
    *,
    lesson_id: str,
    event: EventType,
    actor: str,
    from_revision: int | None,
    to_revision: int,
    payload: dict[str, object],
    at: float,
) -> None:
    last = _rows(db, "select * from events order by seq desc limit 1")
    prev_hash = _row_hash(last[0]) if last else GENESIS_HASH
    db.execute(
        "insert into events (lesson_id,type,actor,from_revision,to_revision,payload,prev_hash,at) "
        "values (?,?,?,?,?,?,?,?)",
        (lesson_id, str(event), actor, from_revision, to_revision, _canonical(payload), prev_hash, at),
    )


def _trigger_json(t: Trigger) -> dict[str, str]:
    return {"kind": str(t.kind), "spec": t.spec, "stage": str(t.stage)}


def _parse_triggers(data: object) -> tuple[Trigger, ...]:
    if not isinstance(data, list):
        raise Rejected(f"triggers must be a list, got {data!r}")
    try:
        return tuple(Trigger(TriggerKind(t["kind"]), str(t["spec"]), Stage(t["stage"])) for t in data)
    except (KeyError, TypeError, ValueError) as exc:
        raise Rejected(f"invalid trigger: {exc}") from None


def _parse_text(data: object) -> LessonText:
    if not isinstance(data, dict):
        raise Rejected(f"text must be an object, got {data!r}")
    try:
        return LessonText(str(data["avoid"]), str(data["instead"]), str(data["reason"]))
    except (KeyError, ValueError) as exc:
        raise Rejected(f"invalid text: {exc}") from None


def _write_triggers(db: sqlite3.Connection, lesson_id: str, triggers: Iterable[Trigger]) -> None:
    db.execute("delete from triggers where lesson_id = ?", (lesson_id,))
    db.executemany(
        "insert or ignore into triggers (lesson_id,kind,spec,stage) values (?,?,?,?)",
        [(lesson_id, str(t.kind), t.spec, str(t.stage)) for t in triggers],
    )


def add_repo(db: sqlite3.Connection, repo_id: RepoId, display_name: str, owner_class: str) -> None:
    """Register a repo, or update its name and owner class."""
    db.execute(
        "insert into repos (id,display_name,owner_class) values (?,?,?) on conflict(id) do update set "
        "display_name = excluded.display_name, owner_class = excluded.owner_class",
        (repo_id, display_name, owner_class),
    )


def create_lesson(
    db: sqlite3.Connection,
    *,
    text: LessonText,
    scope: Scope,
    trust_class: TrustClass,
    triggers: Iterable[Trigger],
    fingerprint: str,
    actor: str,
    fix_program: str | None = None,
    at: float | None = None,
) -> LessonId:
    """Insert a new candidate lesson, its triggers and its `proposed` event in one transaction.

    Raises `Conflict` when an active lesson already has this fingerprint, and `Rejected` for machine scope.
    """
    at = time.time() if at is None else at
    triggers = tuple(triggers)
    lesson_id = LessonId("L-" + secrets.token_hex(4))
    payload: dict[str, object] = {
        "text": {"avoid": text.avoid, "instead": text.instead, "reason": text.reason},
        "scope": scope_to_json(scope),
        "trust_class": str(trust_class),
        "triggers": [_trigger_json(t) for t in triggers],
        "fingerprint": fingerprint,
    }
    state = next_state(None, EventType.PROPOSED, payload)
    cols = _scope_columns(scope)
    db.execute("begin immediate")
    try:
        try:
            db.execute(
                "insert into lessons (id,revision,status,level,pinned,scope_kind,repo_id,path_glob,"
                "session_id,avoid,instead,reason,trust_class,fingerprint,fix_programs,created_at,updated_at,"
                "last_seen_at,expires_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lesson_id,
                    state.revision,
                    str(state.status),
                    str(state.level),
                    int(state.pinned),
                    cols["scope_kind"],
                    cols["repo_id"],
                    cols["path_glob"],
                    cols["session_id"],
                    text.avoid,
                    text.instead,
                    text.reason,
                    str(trust_class),
                    fingerprint,
                    _canonical([fix_program] if fix_program else []),
                    at,
                    at,
                    at,
                    at + EXPIRY_SECONDS,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "fingerprint" in str(exc):
                raise Conflict(f"an active lesson already has fingerprint {fingerprint}") from None
            raise
        _write_triggers(db, lesson_id, triggers)
        _append_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PROPOSED,
            actor=actor,
            from_revision=None,
            to_revision=state.revision,
            payload=payload,
            at=at,
        )
        db.execute("commit")
    except BaseException:
        _rollback(db)
        raise
    return lesson_id


def _state_of(row: sqlite3.Row) -> State:
    return State(
        Status(row["status"]), Level(row["level"]), bool(row["pinned"]), row["scope_kind"], row["revision"]
    )


def _check_block(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    if TrustClass(row["trust_class"]) in NO_BLOCK_TRUST:
        raise Rejected(f"trust class {row['trust_class']} can never reach level block")
    run = db.execute(
        "select matched_failing, matched_passing from precision_runs where lesson_id = ? and revision = ?",
        (row["id"], row["revision"]),
    ).fetchone()
    if run is None:
        raise Rejected(f"level block needs a precision run for revision {row['revision']}")
    failing, passing = run
    if not (failing >= BLOCK_MIN_FAILING and passing < BLOCK_MAX_PASSING_SHARE * (failing + passing)):
        raise Rejected(f"precision run fails the block rule: failing={failing}, passing={passing}")


def apply_event(
    db: sqlite3.Connection,
    *,
    lesson_id: LessonId,
    event: EventType,
    expected_revision: int,
    actor: str,
    payload: dict[str, object],
    at: float | None = None,
) -> int:
    """Apply one lifecycle event in one transaction and return the new revision.

    Payload keys by event: `observed` takes an optional `fix_program`, `promoted` an optional `scope`,
    `level_set` needs `level`, `edited` takes optional `text` and `triggers`, and `superseded` needs
    `superseded_by`.
    """
    if event is EventType.PROPOSED:
        raise Rejected("use create_lesson to propose a lesson")
    at = time.time() if at is None else at
    db.execute("begin immediate")
    try:
        found = _rows(db, "select * from lessons where id = ?", (lesson_id,))
        if not found:
            raise Rejected(f"no lesson {lesson_id}")
        row = found[0]
        if row["revision"] != expected_revision:
            raise Conflict(f"{lesson_id} is at revision {row['revision']}, expected {expected_revision}")
        new = next_state(_state_of(row), event, payload)
        sets: dict[str, object] = {
            "status": str(new.status),
            "level": str(new.level),
            "pinned": int(new.pinned),
            "revision": new.revision,
            "updated_at": at,
        }
        expires_at = row["expires_at"]
        match event:
            case EventType.OBSERVED:
                sets["sessions_seen"] = row["sessions_seen"] + 1
                sets["last_seen_at"] = at
                expires_at = at + EXPIRY_SECONDS
                programs = json.loads(row["fix_programs"])
                fix_program = payload.get("fix_program")
                if isinstance(fix_program, str) and fix_program not in programs:
                    sets["fix_programs"] = _canonical([*programs, fix_program])
            case EventType.PROMOTED if "scope" in payload:
                scope = _parse_scope(payload["scope"])
                if scope.kind == "session":
                    raise Rejected("promotion needs repo, path or machine scope")
                sets.update(_scope_columns(scope))
            case EventType.LEVEL_SET if new.level is Level.BLOCK:
                _check_block(db, row)
            case EventType.EDITED:
                sets["precision_ok"] = 0
                if "text" in payload:
                    text = _parse_text(payload["text"])
                    sets.update(avoid=text.avoid, instead=text.instead, reason=text.reason)
                if "triggers" in payload:
                    _write_triggers(db, lesson_id, _parse_triggers(payload["triggers"]))
            case EventType.SUPERSEDED:
                successor = payload["superseded_by"]
                exists = db.execute("select 1 from lessons where id = ?", (successor,)).fetchone()
                if successor == lesson_id or exists is None:
                    raise Rejected(f"superseded_by {successor!r} is not another lesson")
                sets["superseded_by"] = successor
        if new.status in EXPIRING:
            sets["expires_at"] = (
                expires_at if expires_at is not None else row["last_seen_at"] + EXPIRY_SECONDS
            )
        else:
            sets["expires_at"] = None
        assignments = ", ".join(f"{name} = ?" for name in sets)
        cur = db.execute(
            f"update lessons set {assignments} where id = ? and revision = ?",
            (*sets.values(), lesson_id, expected_revision),
        )
        if cur.rowcount != 1:
            raise Conflict(f"{lesson_id} changed during the update")
        _append_event(
            db,
            lesson_id=lesson_id,
            event=event,
            actor=actor,
            from_revision=expected_revision,
            to_revision=new.revision,
            payload=payload,
            at=at,
        )
        db.execute("commit")
    except BaseException:
        _rollback(db)
        raise
    return new.revision


def verify(db: sqlite3.Connection) -> list[str]:
    """Walk the hash chain and replay every lesson's events. Each string is one problem; empty is healthy."""
    problems: list[str] = []
    expected_hash = GENESIS_HASH
    replayed: dict[str, State | None] = {}
    for row in _rows(db, "select * from events order by seq"):
        seq, lesson_id = row["seq"], row["lesson_id"]
        if row["prev_hash"] != expected_hash:
            problems.append(f"event {seq}: prev_hash does not extend the chain")
        expected_hash = _row_hash(row)
        if lesson_id is None or (lesson_id in replayed and replayed[lesson_id] is None):
            continue
        state = replayed.get(lesson_id)
        try:
            new = next_state(state, EventType(row["type"]), json.loads(row["payload"]))
        except (Rejected, ValueError) as exc:
            problems.append(f"event {seq}: {lesson_id} cannot replay {row['type']}: {exc}")
            replayed[lesson_id] = None
            continue
        expected_from = state.revision if state else None
        if (row["from_revision"], row["to_revision"]) != (expected_from, new.revision):
            problems.append(
                f"event {seq}: {lesson_id} revisions {row['from_revision']}->{row['to_revision']}, "
                f"expected {expected_from}->{new.revision}"
            )
        replayed[lesson_id] = new
    lessons = {row["id"]: row for row in _rows(db, "select * from lessons order by id")}
    for lesson_id, row in lessons.items():
        if lesson_id not in replayed:
            problems.append(f"{lesson_id}: has no events")
            continue
        state = replayed[lesson_id]
        if state is None:
            continue
        actual = _state_of(row)
        for field in ("status", "revision", "pinned", "level", "scope_kind"):
            have, want = getattr(actual, field), getattr(state, field)
            if have != want:
                problems.append(f"{lesson_id}: {field} is {have} in lessons but {want} from events")
    for lesson_id in sorted(set(replayed) - set(lessons)):
        problems.append(f"{lesson_id}: has events but no lessons row")
    return problems


def _load(db: sqlite3.Connection, where: str, params: Iterable[object]) -> list[Lesson]:
    rows = _rows(db, f"select * from lessons where {where} order by id", params)
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    by_lesson: dict[str, list[Trigger]] = {i: [] for i in ids}
    placeholders = ",".join("?" * len(ids))
    for t in _rows(
        db,
        f"select lesson_id, kind, spec, stage from triggers where lesson_id in ({placeholders}) "
        "order by lesson_id, kind, stage, spec",
        ids,
    ):
        by_lesson[t["lesson_id"]].append(Trigger(TriggerKind(t["kind"]), t["spec"], Stage(t["stage"])))
    return [
        Lesson(
            id=LessonId(row["id"]),
            revision=row["revision"],
            status=Status(row["status"]),
            level=Level(row["level"]),
            pinned=bool(row["pinned"]),
            scope=_scope_from_row(row),
            text=LessonText(row["avoid"], row["instead"], row["reason"]),
            trust_class=TrustClass(row["trust_class"]),
            triggers=tuple(by_lesson[row["id"]]),
            fingerprint=row["fingerprint"],
            sessions_seen=row["sessions_seen"],
        )
        for row in rows
    ]


def get_lesson(db: sqlite3.Connection, lesson_id: LessonId) -> Lesson | None:
    found = _load(db, "id = ?", (lesson_id,))
    return found[0] if found else None


def get_lesson_by_fingerprint(db: sqlite3.Connection, fingerprint: str) -> Lesson | None:
    """The active lesson with this fingerprint, which the miner uses to merge sightings."""
    found = _load(db, "fingerprint = ? and status in ('candidate','provisional','promoted')", (fingerprint,))
    return found[0] if found else None


ACTIVE_FOR_HOOK_WHERE = (
    "(status in ('provisional','promoted') and scope_kind = 'machine')"
    " or (status in ('provisional','promoted') and scope_kind in ('repo','path') and repo_id = ?)"
    " or (status = 'candidate' and scope_kind = 'session' and session_id = ?)"
)


def active_for_hook(db: sqlite3.Connection, repo_id: RepoId | None, session_id: SessionId) -> list[Lesson]:
    """Promoted and provisional lessons in machine scope or this repo's repo and path scopes, plus this
    session's session-scoped candidates. Stage rules (pins, levels, trigger kinds) are the caller's job."""
    return _load(db, ACTIVE_FOR_HOOK_WHERE, (repo_id, session_id))


def inbox_candidates(db: sqlite3.Connection) -> list[Lesson]:
    """Every candidate, for `scars inbox` to rank."""
    return _load(db, "status = 'candidate'", ())
