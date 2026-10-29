"""SQLite persistence for lessons.db and telemetry.db."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
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
    """Create lessons.db (mode 0600, parent 0700) with the schema if it is new, and return a connection.

    Safe to call on an existing store: the schema is applied once, under the write lock.
    """
    return _init(path, "lessons", "FULL")


def init_telemetry(path: Path) -> sqlite3.Connection:
    """Create telemetry.db like `init_lessons`, with `synchronous = NORMAL`: hooks write it best-effort."""
    return _init(path, "telemetry", "NORMAL")


class Conflict(Exception):
    """A compare-and-set update found a newer revision than the caller expected."""


class Rejected(Exception):
    """The lifecycle table forbids this event for the lesson's current status."""


@dataclass(frozen=True)
class State:
    """Every lesson field the event log determines. `apply_event` writes it and `verify` recomputes it."""

    status: Status
    level: Level
    pinned: bool
    scope: Scope
    revision: int
    text: LessonText
    trust_class: TrustClass
    triggers: frozenset[Trigger]
    fingerprint: str
    sessions_seen: int
    fix_programs: tuple[str, ...]
    superseded_by: str | None
    created_at: float
    updated_at: float
    last_seen_at: float


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


@dataclass(frozen=True)
class PayloadKey:
    kind: type
    required: bool = False


PAYLOAD_KEYS: dict[EventType, dict[str, PayloadKey]] = {
    EventType.PROPOSED: {
        "text": PayloadKey(dict, required=True),
        "scope": PayloadKey(dict, required=True),
        "trust_class": PayloadKey(str, required=True),
        "triggers": PayloadKey(list, required=True),
        "fingerprint": PayloadKey(str, required=True),
        "fix_program": PayloadKey(str),
    },
    EventType.OBSERVED: {"fix_program": PayloadKey(str)},
    EventType.PROMOTED: {"scope": PayloadKey(dict)},
    EventType.LEVEL_SET: {"level": PayloadKey(str, required=True)},
    EventType.EDITED: {"text": PayloadKey(dict), "triggers": PayloadKey(list), "scope": PayloadKey(dict)},
    EventType.SUPERSEDED: {"superseded_by": PayloadKey(str, required=True)},
}
"""The payload keys each event accepts. Events not listed take an empty payload."""

_TYPE_NAMES = {str: "a string", dict: "an object", list: "a list"}


def parse_payload(event: EventType, data: object) -> dict[str, object]:
    """Check a payload against `PAYLOAD_KEYS`, or raise `Rejected` naming the first bad key."""
    if not isinstance(data, dict):
        raise Rejected(f"payload must be an object, got {data!r}")
    keys = PAYLOAD_KEYS.get(event, {})
    for name, key in keys.items():
        if name not in data:
            if key.required:
                raise Rejected(f"payload is missing {name}")
        elif not isinstance(data[name], key.kind):
            raise Rejected(f"payload {name} must be {_TYPE_NAMES[key.kind]}, got {data[name]!r}")
    for name in sorted(map(str, data)):
        if name not in keys:
            raise Rejected(f"payload has unknown key {name!r}")
    return data


def next_state(state: State | None, event: EventType, payload: object, *, at: float) -> State:
    """Apply one event, recorded at time `at`, to a lesson's state, or raise `Rejected`.

    Pure; shared by writes and `verify`, so both accept exactly the payloads `parse_payload` accepts.
    """
    payload = parse_payload(event, payload)
    rule = LIFECYCLE[event]
    if event is EventType.PROPOSED:
        if state is not None:
            raise Rejected("proposed applies only to a new lesson")
        return _proposed(payload, at)
    if state is None:
        raise Rejected(f"{event} needs an existing lesson")
    if state.status not in rule.allowed_from:
        raise Rejected(f"{event} is not allowed from {state.status}")
    new = replace(state, status=rule.to or state.status, revision=state.revision + 1, updated_at=at)
    match event:
        case EventType.OBSERVED:
            new = replace(new, sessions_seen=state.sessions_seen + 1, last_seen_at=at)
            fix_program = payload.get("fix_program")
            if isinstance(fix_program, str) and fix_program not in state.fix_programs:
                new = replace(new, fix_programs=(*state.fix_programs, fix_program))
        case EventType.PROMOTED:
            scope = _parse_scope(payload["scope"]) if "scope" in payload else state.scope
            if scope.kind == "session":
                raise Rejected("promotion needs repo, path or machine scope, not session scope")
            new = replace(new, scope=scope)
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
            if "text" in payload:
                new = replace(new, text=_parse_text(payload["text"]))
            if "triggers" in payload:
                new = replace(new, triggers=frozenset(_parse_triggers(payload["triggers"])))
            if "scope" in payload:
                scope = _parse_scope(payload["scope"])
                if scope.kind == "machine" and state.scope.kind != "machine":
                    raise Rejected("an edit cannot move a lesson into machine scope; promote it instead")
                new = replace(new, scope=scope)
            # ADR-0004: a machine-scope lesson cannot be a candidate, so the edit keeps it promoted.
            if new.scope.kind == "machine":
                new = replace(new, status=Status.PROMOTED)
        case EventType.SUPERSEDED:
            successor = payload.get("superseded_by")
            if not isinstance(successor, str):
                raise Rejected("superseded needs superseded_by")
            new = replace(new, superseded_by=successor)
    if new.status is not Status.PROMOTED:
        new = replace(new, pinned=False, level=Level.NOTE)
    return new


def _proposed(payload: dict[str, object], at: float) -> State:
    scope = _parse_scope(payload.get("scope"))
    if scope.kind == "machine":
        raise Rejected("a machine-scope lesson cannot be a candidate")
    try:
        trust_class = TrustClass(payload.get("trust_class"))
    except ValueError:
        raise Rejected(f"invalid trust_class {payload.get('trust_class')!r}") from None
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise Rejected(f"invalid fingerprint {fingerprint!r}")
    fix_program = payload.get("fix_program")
    return State(
        status=Status.CANDIDATE,
        level=Level.NOTE,
        pinned=False,
        scope=scope,
        revision=1,
        text=_parse_text(payload.get("text")),
        trust_class=trust_class,
        triggers=frozenset(_parse_triggers(payload.get("triggers"))),
        fingerprint=fingerprint,
        sessions_seen=1,
        fix_programs=(fix_program,) if isinstance(fix_program, str) and fix_program else (),
        superseded_by=None,
        created_at=at,
        updated_at=at,
        last_seen_at=at,
    )


def _columns(state: State) -> dict[str, object]:
    """The `lessons` columns a state determines, as stored. Writes and `verify` share this mapping.

    ADR-0004: candidate and provisional lessons expire 14 days after their last sighting, others never.
    """
    return {
        "revision": state.revision,
        "status": str(state.status),
        "level": str(state.level),
        "pinned": int(state.pinned),
        **_scope_columns(state.scope),
        "avoid": state.text.avoid,
        "instead": state.text.instead,
        "reason": state.text.reason,
        "trust_class": str(state.trust_class),
        "fingerprint": state.fingerprint,
        "sessions_seen": state.sessions_seen,
        "fix_programs": _canonical(list(state.fix_programs)),
        "superseded_by": state.superseded_by,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "last_seen_at": state.last_seen_at,
        "expires_at": state.last_seen_at + EXPIRY_SECONDS if state.status in EXPIRING else None,
    }


def _trigger_keys(triggers: Iterable[Trigger]) -> list[tuple[object, ...]]:
    return _sorted_keys((str(t.kind), t.spec, str(t.stage)) for t in triggers)


def _sorted_keys(keys: Iterable[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return sorted(set(keys), key=lambda key: tuple(map(str, key)))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _blob_json(value: object) -> object:
    # Writes never store blobs, but a tampered column can; hash its bytes so verify reports it.
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    raise TypeError(f"cannot hash {type(value).__name__}")


def _row_hash(row: sqlite3.Row) -> str:
    text = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=_blob_json)
    return hashlib.sha256(text.encode()).hexdigest()


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
    return tuple(map(_parse_trigger, data))


def _parse_trigger(data: object) -> Trigger:
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("kind", "spec", "stage")):
        raise Rejected(f"invalid trigger {data!r}")
    try:
        return Trigger(TriggerKind(data["kind"]), data["spec"], Stage(data["stage"]))
    except ValueError:
        raise Rejected(f"invalid trigger {data!r}") from None


def _parse_text(data: object) -> LessonText:
    if not isinstance(data, dict):
        raise Rejected(f"text must be an object, got {data!r}")
    for key in ("avoid", "instead", "reason"):
        if key not in data:
            raise Rejected(f"text is missing {key}")
        if not isinstance(data[key], str):
            raise Rejected(f"text {key} must be a string, got {data[key]!r}")
    try:
        return LessonText(str(data["avoid"]), str(data["instead"]), str(data["reason"]))
    except ValueError as exc:
        raise Rejected(f"invalid text: {exc}") from None


def _decode_payload(raw: object) -> object:
    if not isinstance(raw, str):
        raise Rejected(f"payload must be JSON text, got {raw!r}")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise Rejected(f"payload is not JSON: {exc}") from None


def _event_at(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise Rejected(f"at must be a number, got {raw!r}")
    return float(raw)


def _write_triggers(db: sqlite3.Connection, lesson_id: str, triggers: Iterable[Trigger]) -> None:
    db.execute("delete from triggers where lesson_id = ?", (lesson_id,))
    db.executemany(
        "insert into triggers (lesson_id,kind,spec,stage) values (?,?,?,?)",
        [(lesson_id, *key) for key in _trigger_keys(triggers)],
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
    if fix_program:
        payload["fix_program"] = fix_program
    state = next_state(None, EventType.PROPOSED, payload, at=at)
    cols: dict[str, object] = {"id": lesson_id, **_columns(state)}
    db.execute("begin immediate")
    try:
        try:
            db.execute(
                f"insert into lessons ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                tuple(cols.values()),
            )
        except sqlite3.IntegrityError as exc:
            if "fingerprint" in str(exc):
                raise Conflict(f"an active lesson already has fingerprint {fingerprint}") from None
            raise
        _write_triggers(db, lesson_id, state.triggers)
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


@contextmanager
def _snapshot(db: sqlite3.Connection) -> Iterator[None]:
    """Run several reads against one snapshot, so a concurrent commit cannot land between them.

    A caller's open transaction already pins a snapshot, so it is left alone.
    """
    if db.in_transaction:
        yield
        return
    db.execute("begin")
    try:
        yield
    finally:
        _rollback(db)


def _triggers_where(db: sqlite3.Connection, where: str, params: Iterable[object]) -> dict[str, list[Trigger]]:
    """Triggers of the lessons matching `where`, grouped by lesson id."""
    out: dict[str, list[Trigger]] = {}
    for t in _rows(
        db,
        f"select lesson_id, kind, spec, stage from triggers where lesson_id in "
        f"(select id from lessons where {where}) order by lesson_id, kind, stage, spec",
        params,
    ):
        out.setdefault(t["lesson_id"], []).append(
            Trigger(TriggerKind(t["kind"]), t["spec"], Stage(t["stage"]))
        )
    return out


def _state_of(row: sqlite3.Row, triggers: Iterable[Trigger]) -> State:
    return State(
        status=Status(row["status"]),
        level=Level(row["level"]),
        pinned=bool(row["pinned"]),
        scope=_scope_from_row(row),
        revision=row["revision"],
        text=LessonText(row["avoid"], row["instead"], row["reason"]),
        trust_class=TrustClass(row["trust_class"]),
        triggers=frozenset(triggers),
        fingerprint=row["fingerprint"],
        sessions_seen=row["sessions_seen"],
        fix_programs=tuple(json.loads(row["fix_programs"])),
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
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

    Payload keys by event: `observed` takes an optional `fix_program`, `promoted` an optional `scope`
    (required when the lesson is session-scoped), `level_set` needs `level`, `edited` takes optional
    `text`, `triggers` and `scope` (never machine scope), and `superseded` needs `superseded_by`.
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
        old = _state_of(row, _triggers_where(db, "id = ?", (lesson_id,)).get(lesson_id, ()))
        new = next_state(old, event, payload, at=at)
        sets = _columns(new)
        match event:
            case EventType.LEVEL_SET if new.level is Level.BLOCK:
                _check_block(db, row)
            case EventType.EDITED:
                sets["precision_ok"] = 0
            case EventType.SUPERSEDED:
                successor = new.superseded_by
                exists = db.execute("select 1 from lessons where id = ?", (successor,)).fetchone()
                if successor == lesson_id or exists is None:
                    raise Rejected(f"superseded_by {successor!r} is not another lesson")
        if new.triggers != old.triggers:
            _write_triggers(db, lesson_id, new.triggers)
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
    """Walk the hash chain and replay every lesson's events. Each string is one problem; empty is healthy.

    Replay rebuilds every column in `_columns` plus the trigger set, and each must match the stored row.
    Timestamps compare exactly: a write stores the same float in the event's `at` and the lesson row,
    and replay redoes the same arithmetic on it. Never raises on stored data: a malformed event is
    reported and ends replay of its lesson, and the other lessons are still checked.
    """
    with _snapshot(db):
        events = _rows(db, "select * from events order by seq")
        lessons = {row["id"]: row for row in _rows(db, "select * from lessons order by id")}
        trigger_rows = db.execute("select lesson_id, kind, spec, stage from triggers").fetchall()
    triggers: dict[object, list[tuple[object, ...]]] = {}
    for lesson_id, *key in trigger_rows:
        triggers.setdefault(lesson_id, []).append(tuple(key))
    problems: list[str] = []
    expected_hash = GENESIS_HASH
    replayed: dict[str, State | None] = {}
    for row in events:
        seq, lesson_id = row["seq"], row["lesson_id"]
        if row["prev_hash"] != expected_hash:
            problems.append(f"event {seq}: prev_hash does not extend the chain")
        expected_hash = _row_hash(row)
        if lesson_id is None or (lesson_id in replayed and replayed[lesson_id] is None):
            continue
        state = replayed.get(lesson_id)
        try:
            event = EventType(row["type"])
            new = next_state(state, event, _decode_payload(row["payload"]), at=_event_at(row["at"]))
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
    for lesson_id, row in lessons.items():
        if lesson_id not in replayed:
            problems.append(f"{lesson_id}: has no events")
            continue
        state = replayed[lesson_id]
        if state is None:
            continue
        for name, want in _columns(state).items():
            if row[name] != want:
                problems.append(f"{lesson_id}: {name} is {row[name]} in lessons but {want} from events")
        have_triggers = _sorted_keys(triggers.get(lesson_id, ()))
        want_triggers = _trigger_keys(state.triggers)
        if have_triggers != want_triggers:
            problems.append(
                f"{lesson_id}: triggers are {have_triggers} in lessons but {want_triggers} from events"
            )
    for lesson_id in sorted(set(replayed) - set(lessons), key=str):
        problems.append(f"{lesson_id}: has events but no lessons row")
    return problems


def _load(db: sqlite3.Connection, where: str, params: Iterable[object]) -> list[Lesson]:
    params = tuple(params)
    with _snapshot(db):
        rows = _rows(db, f"select * from lessons where {where} order by id", params)
        if not rows:
            return []
        by_lesson = _triggers_where(db, where, params)
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
            triggers=tuple(by_lesson.get(row["id"], ())),
            fingerprint=row["fingerprint"],
            sessions_seen=row["sessions_seen"],
        )
        for row in rows
    ]


def get_lesson(db: sqlite3.Connection, lesson_id: LessonId) -> Lesson | None:
    """The lesson with this id in any status, with its triggers, or None when no such lesson exists."""
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
