"""SQLite persistence for lessons.db and telemetry.db.

`hashlib`, `secrets`, `importlib.resources`, `urllib.parse` and `pathlib` are imported inside the
functions that need them, because the hook imports this module on its latency budget and needs none of
them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1
TELEMETRY_SCHEMA_VERSION = 2
"""2 allows the `ignored` hit outcome, which a version 1 hits table rejects."""
EXPIRY_SECONDS = 14 * 86400
GENESIS_HASH = "0" * 64
BLOCK_MIN_FAILING = 3
BLOCK_MAX_PASSING_SHARE = 0.01
NO_BLOCK_TRUST = frozenset({TrustClass.THIRD_PARTY_REPO, TrustClass.WEB})
EXPIRING = frozenset({Status.CANDIDATE, Status.PROVISIONAL})


def schema_sql(name: str) -> str:
    """The DDL for `lessons` or `telemetry`, shipped in `scars/schema/`."""
    from importlib.resources import files

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
        from urllib.parse import quote

        uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
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


def _init(path: Path, schema: str, synchronous: str, version: int = SCHEMA_VERSION) -> sqlite3.Connection:
    from pathlib import Path

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
            db.execute(f"pragma user_version = {int(version)}")
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
    return _init(path, "telemetry", "NORMAL", TELEMETRY_SCHEMA_VERSION)


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
    EventType.EDITED: {
        "text": PayloadKey(dict),
        "triggers": PayloadKey(list),
        "scope": PayloadKey(dict),
        "trust_class": PayloadKey(str),
    },
    EventType.SUPERSEDED: {"superseded_by": PayloadKey(str, required=True)},
    EventType.RETIRED: {"reason": PayloadKey(str)},
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
            if "trust_class" in payload:
                # A human rewrite makes the text the human's, and nothing else may change trust.
                if payload["trust_class"] != TrustClass.HUMAN.value:
                    raise Rejected(f"an edit can only set trust class human, not {payload['trust_class']!r}")
                new = replace(new, trust_class=TrustClass.HUMAN)
            if "scope" in payload:
                scope = _parse_scope(payload["scope"])
                if scope.kind == "machine" and state.scope.kind != "machine":
                    raise Rejected("an edit cannot move a lesson into machine scope; promote it instead")
                new = replace(new, scope=scope)
            # A machine-scope lesson cannot be a candidate, so the edit keeps it promoted.
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

    Candidate and provisional lessons expire 14 days after their last sighting, others never.
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


@dataclass(frozen=True)
class InvalidText:
    """A stored TEXT value whose bytes are not valid UTF-8, as `verify` reads it."""

    raw: bytes

    def __str__(self) -> str:
        return f"<invalid UTF-8 {self.raw.hex()}>"


def _decode_text(raw: bytes) -> str | InvalidText:
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return InvalidText(raw)


def _blob_json(value: object) -> object:
    # Writes never store blobs or invalid text, but a tampered column can; hash its bytes so verify
    # reports it. Neither shape can equal the JSON string a valid value hashes as.
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if isinstance(value, InvalidText):
        return {"invalid_utf8": value.raw.hex()}
    raise TypeError(f"cannot hash {type(value).__name__}")


def _row_hash(row: sqlite3.Row) -> str:
    text = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=_blob_json)
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _rows(db: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, tuple(params)).fetchall()


def _rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("rollback")


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    """Run several store writes as one transaction: all of them commit, or none does.

    `create_lesson` and `apply_event` called inside it join the transaction instead of committing on
    their own, and a `Conflict` or `Rejected` they raise undoes only their own change.
    """
    db.execute("begin immediate")
    try:
        yield
        db.execute("commit")
    except BaseException:
        _rollback(db)
        raise


@contextmanager
def _write_unit(db: sqlite3.Connection) -> Iterator[None]:
    """One write unit: its own transaction, or a savepoint inside a caller's `transaction`."""
    if not db.in_transaction:
        with transaction(db):
            yield
        return
    db.execute("savepoint store_write")
    try:
        yield
    except BaseException:
        db.execute("rollback to store_write")
        db.execute("release store_write")
        raise
    db.execute("release store_write")


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
    except RecursionError:
        raise Rejected("payload nests too deeply") from None


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
    import secrets

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
    with _write_unit(db):
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
    `text`, `triggers`, `scope` (never machine scope) and `trust_class` (only `human`), and `superseded`
    needs `superseded_by`.
    """
    if event is EventType.PROPOSED:
        raise Rejected("use create_lesson to propose a lesson")
    at = time.time() if at is None else at
    with _write_unit(db):
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
    return new.revision


def verify(db: sqlite3.Connection) -> list[str]:
    """Walk the hash chain and replay every lesson's events. Each string is one problem; empty is healthy.

    Replay rebuilds every column in `_columns` plus the trigger set, and each must match the stored row.
    Timestamps compare exactly: a write stores the same float in the event's `at` and the lesson row,
    and replay redoes the same arithmetic on it. Never raises on stored data: a malformed event is
    reported and ends replay of its lesson, and the other lessons are still checked.

    TEXT that is not valid UTF-8 is reported by table, rowid and column. Such an event ends its
    lesson's replay, such a lessons cell is not compared, and such a trigger row is left out of the
    trigger set. Valid text hashes exactly as the default decoder reads it, so existing chains hold.
    """
    factory = db.text_factory
    db.text_factory = _decode_text
    try:
        with _snapshot(db):
            events = _rows(db, "select * from events order by seq")
            lesson_rows = _rows(db, "select rowid as row_id, * from lessons order by id")
            trigger_rows = _rows(
                db, "select rowid as row_id, lesson_id, kind, spec, stage from triggers order by rowid"
            )
    finally:
        db.text_factory = factory
    problems = [
        f"{table} row {row[key]}: {name} is not valid UTF-8"
        for table, key, rows in (
            ("events", "seq", events),
            ("lessons", "row_id", lesson_rows),
            ("triggers", "row_id", trigger_rows),
        )
        for row in rows
        for name, value in zip(row.keys(), row, strict=True)
        if isinstance(value, InvalidText)
    ]
    lessons = {row["id"]: row for row in lesson_rows}
    triggers: dict[object, list[tuple[object, ...]]] = {}
    for t in trigger_rows:
        if not any(isinstance(value, InvalidText) for value in t):
            triggers.setdefault(t["lesson_id"], []).append((t["kind"], t["spec"], t["stage"]))
    expected_hash = GENESIS_HASH
    replayed: dict[str, State | None] = {}
    for row in events:
        seq, lesson_id = row["seq"], row["lesson_id"]
        if row["prev_hash"] != expected_hash:
            problems.append(f"event {seq}: prev_hash does not extend the chain")
        expected_hash = _row_hash(row)
        if lesson_id is None or (lesson_id in replayed and replayed[lesson_id] is None):
            continue
        if any(isinstance(value, InvalidText) for value in row):
            replayed[lesson_id] = None
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
            if row[name] != want and not isinstance(row[name], InvalidText):
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
    "(status = 'promoted' and scope_kind = 'machine')"
    " or (status = 'promoted' and scope_kind in ('repo','path') and repo_id = ?)"
)


def active_for_hook(db: sqlite3.Connection, repo_id: RepoId | None) -> list[Lesson]:
    """Promoted lessons in machine scope or this repo's repo and path scopes: only a lesson a human
    approved reaches an agent. Stage rules (pins, levels, trigger kinds) are the caller's job."""
    return _load(db, ACTIVE_FOR_HOOK_WHERE, (repo_id,))


def active_for_stage(
    db: sqlite3.Connection,
    repo_id: RepoId | None,
    *,
    stage: Stage,
    pinned_only: bool,
    trigger_kinds: Iterable[TriggerKind] | None,
    error_signature: str | None = None,
) -> list[Lesson]:
    """`active_for_hook`, narrowed in SQL to lessons that can fire at one stage, so the hook builds no
    others.

    With `pinned_only`, only pinned lessons. With `trigger_kinds`, only lessons with a trigger of one of
    those kinds at `stage`; with `error_signature` too, an `error` trigger counts only when its spec is
    exactly that signature, which is the only way an error trigger fires. Each lesson keeps all its
    triggers. With `trigger_kinds` None the stage selects without triggers, so no trigger is required.
    """
    where = f"({ACTIVE_FOR_HOOK_WHERE})"
    params: list[object] = [repo_id]
    if pinned_only:
        where += " and pinned = 1"
    if trigger_kinds is not None:
        kinds = sorted(k.value for k in trigger_kinds)
        fires = f"t.stage = ? and t.kind in ({','.join('?' * len(kinds))})"
        params += [stage.value, *kinds]
        if error_signature is not None:
            fires += " and (t.kind <> 'error' or t.spec = ?)"
            params.append(error_signature)
        where += f" and exists (select 1 from triggers t where t.lesson_id = lessons.id and {fires})"
    return _load(db, where, params)


def inbox_candidates(db: sqlite3.Connection) -> list[Lesson]:
    """Every candidate, for `scars inbox` to rank."""
    return _load(db, "status = 'candidate'", ())


def repo_for_common_dir(db: sqlite3.Connection, common_dirs: Iterable[str]) -> RepoId | None:
    """The registered repo of the first of these git common dirs that is a registered clone, or None."""
    for common_dir in common_dirs:
        row = db.execute("select repo_id from clones where common_dir = ?", (common_dir,)).fetchone()
        if row is not None:
            return RepoId(row[0])
    return None


def repo_names(db: sqlite3.Connection) -> tuple[str, ...]:
    """Display names of every registered repo, for the machine-scope lint."""
    return tuple(r[0] for r in db.execute("select display_name from repos order by id"))


def passing_inputs(db: sqlite3.Connection, lesson_ids: Iterable[LessonId]) -> dict[LessonId, frozenset[str]]:
    """Each lesson's observed passing inputs: the commands render accepts in its `instead` field.

    Every requested id is a key, with an empty set when the lesson has no observation.
    """
    ids = list(dict.fromkeys(lesson_ids))
    found: dict[LessonId, set[str]] = {i: set() for i in ids}
    if ids:
        marks = ",".join("?" * len(ids))
        sql = f"select lesson_id, passing_input from observations where lesson_id in ({marks})"
        for lesson_id, passing in db.execute(sql, ids):
            found[LessonId(lesson_id)].add(passing)
    return {i: frozenset(v) for i, v in found.items()}


def promoted_at(db: sqlite3.Connection, lesson_ids: Iterable[LessonId]) -> dict[LessonId, float]:
    """The time of each lesson's latest `promoted` event, for the `reviewed <date>` header.

    Lessons that were never promoted are absent.
    """
    ids = list(dict.fromkeys(lesson_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    sql = (
        f"select lesson_id, max(at) from events where type = 'promoted' and lesson_id in ({marks}) "
        "group by lesson_id"
    )
    return {LessonId(lesson_id): at for lesson_id, at in db.execute(sql, ids)}


# ---------------------------------------------------------------- miner state


def ensure_miner_schema(db: sqlite3.Connection) -> None:
    """Create the miner's later tables (`mined_calls`, `miner_file_context`) in a store made before they
    existed. Idempotent."""
    for statement in _statements(schema_sql("lessons")):
        if "if not exists" in statement:
            db.execute(statement)


def ensure_telemetry_schema(db: sqlite3.Connection) -> int:
    """Bring telemetry.db up to `TELEMETRY_SCHEMA_VERSION` and add later indexes. Idempotent.

    The upgrades run in order inside one write transaction, so a failure leaves the file as it was. Only
    the CLI calls this; the hook never upgrades and keeps dropping a write the old schema rejects.
    Returns the version the file had before.
    """
    db.execute("begin immediate")
    try:
        found = db.execute("pragma user_version").fetchone()[0]
        if 0 < found < TELEMETRY_SCHEMA_VERSION:
            for version in range(found + 1, TELEMETRY_SCHEMA_VERSION + 1):
                _TELEMETRY_UPGRADES[version](db)
            db.execute(f"pragma user_version = {TELEMETRY_SCHEMA_VERSION}")
        for statement in _statements(schema_sql("telemetry")):
            if "if not exists" in statement:
                db.execute(statement)
        db.execute("commit")
    except BaseException:
        _rollback(db)
        raise
    return found


def _rebuild_hits(db: sqlite3.Connection) -> None:
    """Recreate `hits` from the current DDL, keeping every row and its `seq`: SQLite cannot alter a CHECK."""
    db.execute("alter table hits rename to hits_old")
    indexes = db.execute(
        "select name from sqlite_master where type = 'index' and tbl_name = 'hits_old' and sql is not null"
    )
    for (name,) in indexes.fetchall():
        db.execute(f'drop index "{name}"')
    for statement in _statements(schema_sql("telemetry")):
        if "create table hits " in statement or " on hits(" in statement:
            db.execute(statement)
    columns = ", ".join(row[1] for row in db.execute("pragma table_info(hits_old)"))
    db.execute(f"insert into hits ({columns}) select {columns} from hits_old")
    db.execute("drop table hits_old")


_TELEMETRY_UPGRADES = {2: _rebuild_hits}


def add_clone(db: sqlite3.Connection, common_dir: str, repo_id: RepoId, at: float | None = None) -> None:
    """Register a clone's git common dir for a registered repo, or move it to that repo."""
    db.execute(
        "insert into clones (common_dir, repo_id, registered_at) values (?,?,?) "
        "on conflict(common_dir) do update set repo_id = excluded.repo_id",
        (common_dir, repo_id, time.time() if at is None else at),
    )


def repo_owner_class(db: sqlite3.Connection, repo_id: RepoId) -> str | None:
    """`own` or `upstream` for a registered repo, or None when it is not registered."""
    row = db.execute("select owner_class from repos where id = ?", (repo_id,)).fetchone()
    return row[0] if row else None


@dataclass(frozen=True)
class Offset:
    path: str
    offset: int
    inode: int
    mtime: float


@dataclass(frozen=True)
class FileContext:
    """Session context a transcript set before its saved offset (Codex `session_meta` and `turn_context`),
    restored when the next batch reads on from that offset."""

    path: str
    cwd: str
    model_id: str | None


def miner_file_contexts(db: sqlite3.Connection) -> dict[str, FileContext]:
    return {r[0]: FileContext(*r) for r in db.execute("select path, cwd, model_id from miner_file_context")}


@dataclass(frozen=True)
class PendingFailure:
    """A failed call still waiting, across batches, for the call that fixes it."""

    tool_use_id: str
    harness: str
    session_id: str
    tool: str
    program: str | None
    input: str
    error_signature: str
    calls_since: int
    last_call_at: float


@dataclass(frozen=True)
class MinedCall:
    """One `mined_calls` row. `command` is None for non-shell calls and for commands that held a secret."""

    tool_use_id: str
    harness: str
    session_id: str
    at: float
    tool: str
    tool_kind: str
    command: str | None
    paths: tuple[str, ...]
    status: str
    """`ok`, `failed`, or `unknown` when the transcript shows no outcome (a Codex call with no exit code)."""
    error_signature: str | None
    cwd: str
    model_id: str | None


@dataclass(frozen=True)
class Observation:
    lesson_id: LessonId
    harness: str
    session_id: str
    failed_tool_use_id: str
    failed_input: str
    error_signature: str
    passing_input: str
    at: float


def miner_offsets(db: sqlite3.Connection) -> dict[str, Offset]:
    return {r[0]: Offset(*r) for r in db.execute("select path, offset, inode, mtime from miner_offsets")}


def seen_tool_uses(db: sqlite3.Connection, ids: Iterable[str]) -> frozenset[str]:
    """The subset of `ids` already fully processed by an earlier batch."""
    wanted = list(dict.fromkeys(ids))
    found: set[str] = set()
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        found.update(
            r[0]
            for r in db.execute(
                f"select tool_use_id from seen_tool_use where tool_use_id in ({marks})", chunk
            )
        )
    return frozenset(found)


def pending_failures(db: sqlite3.Connection) -> list[PendingFailure]:
    rows = db.execute(
        "select tool_use_id, harness, session_id, tool, program, input, error_signature, calls_since, "
        "last_call_at from pending_failures order by last_call_at, tool_use_id"
    )
    return [PendingFailure(*r) for r in rows]


def _mined_call(row: tuple[object, ...]) -> MinedCall:
    values = list(row)
    values[7] = tuple(json.loads(str(values[7])))
    return MinedCall(*values)  # type: ignore[arg-type]


_CALL_COLUMNS = (
    "tool_use_id, harness, session_id, at, tool, tool_kind, command, paths, status, "
    "error_signature, cwd, model_id"
)


def mined_calls_since(db: sqlite3.Connection, since: float) -> list[MinedCall]:
    """Every mined call at or after `since`, in transcript order."""
    rows = db.execute(f"select {_CALL_COLUMNS} from mined_calls where at >= ? order by seq", (since,))
    return [_mined_call(r) for r in rows]


def session_calls(db: sqlite3.Connection, session_id: str) -> list[MinedCall]:
    """One session's mined calls in transcript order."""
    rows = db.execute(
        f"select {_CALL_COLUMNS} from mined_calls where session_id = ? order by seq", (session_id,)
    )
    return [_mined_call(r) for r in rows]


def mined_call(db: sqlite3.Connection, tool_use_id: str) -> MinedCall | None:
    row = db.execute(
        f"select {_CALL_COLUMNS} from mined_calls where tool_use_id = ?", (tool_use_id,)
    ).fetchone()
    return _mined_call(row) if row else None


def has_observation(db: sqlite3.Connection, failed_tool_use_id: str) -> bool:
    found = db.execute("select 1 from observations where failed_tool_use_id = ?", (failed_tool_use_id,))
    return found.fetchone() is not None


def observed_in_session(db: sqlite3.Connection, lesson_id: LessonId, harness: str, session_id: str) -> bool:
    """Whether this lesson already has an observation from this session."""
    found = db.execute(
        "select 1 from observations where lesson_id = ? and harness = ? and session_id = ?",
        (lesson_id, harness, session_id),
    )
    return found.fetchone() is not None


def add_observation(db: sqlite3.Connection, obs: Observation) -> bool:
    """Insert one observation; False when one already exists for this failed call."""
    cur = db.execute(
        "insert or ignore into observations (lesson_id, harness, session_id, failed_tool_use_id, "
        "failed_input, error_signature, passing_input, at) values (?,?,?,?,?,?,?,?)",
        (
            obs.lesson_id,
            obs.harness,
            obs.session_id,
            obs.failed_tool_use_id,
            obs.failed_input,
            obs.error_signature,
            obs.passing_input,
            obs.at,
        ),
    )
    return cur.rowcount == 1


def record_precision(
    db: sqlite3.Connection,
    lesson_id: LessonId,
    revision: int,
    *,
    matched_failing: int,
    matched_passing: int,
    window_days: int,
    at: float,
) -> None:
    """Store one precision run for a lesson revision, replacing an earlier run of the same revision."""
    db.execute(
        "insert or replace into precision_runs (lesson_id, revision, matched_failing, matched_passing, "
        "window_days, at) values (?,?,?,?,?,?)",
        (lesson_id, revision, matched_failing, matched_passing, window_days, at),
    )


def lesson_last_seen(db: sqlite3.Connection, lesson_id: LessonId) -> float | None:
    row = db.execute("select last_seen_at from lessons where id = ?", (lesson_id,)).fetchone()
    return row[0] if row else None


def lessons_last_seen(db: sqlite3.Connection) -> dict[LessonId, float | None]:
    """Every lesson's `last_seen_at` in one query, for listings that would otherwise ask per lesson."""
    return {LessonId(i): at for i, at in db.execute("select id, last_seen_at from lessons")}


def expiring_lessons(db: sqlite3.Connection, now: float) -> list[tuple[LessonId, int]]:
    """`(id, revision)` of every candidate and provisional lesson whose expiry time has passed."""
    rows = db.execute(
        "select id, revision from lessons where status in ('candidate','provisional') "
        "and expires_at is not null and expires_at <= ? order by id",
        (now,),
    )
    return [(LessonId(i), r) for i, r in rows]


def commit_batch(
    db: sqlite3.Connection,
    *,
    offsets: Iterable[Offset],
    seen: Iterable[str],
    pending: Iterable[PendingFailure],
    calls: Iterable[MinedCall],
    prune_before: float,
    contexts: Iterable[FileContext] = (),
) -> None:
    """Advance the miner in one transaction: new file offsets, processed tool results, the full set of
    still-pending failures (replacing the old set), new mined calls, and calls older than `prune_before`
    removed, and the session context saved with each offset. A crash leaves either the old state or the
    new one, so a batch is never half-consumed."""
    db.execute("begin immediate")
    try:
        db.executemany(
            "insert or replace into miner_offsets (path, offset, inode, mtime) values (?,?,?,?)",
            [(o.path, o.offset, o.inode, o.mtime) for o in offsets],
        )
        db.executemany(
            "insert or replace into miner_file_context (path, cwd, model_id) values (?,?,?)",
            [(c.path, c.cwd, c.model_id) for c in contexts],
        )
        db.executemany("insert or ignore into seen_tool_use (tool_use_id) values (?)", [(s,) for s in seen])
        db.execute("delete from pending_failures")
        db.executemany(
            "insert into pending_failures (tool_use_id, harness, session_id, tool, program, input, "
            "error_signature, calls_since, last_call_at) values (?,?,?,?,?,?,?,?,?)",
            [
                (
                    p.tool_use_id,
                    p.harness,
                    p.session_id,
                    p.tool,
                    p.program,
                    p.input,
                    p.error_signature,
                    p.calls_since,
                    p.last_call_at,
                )
                for p in pending
            ],
        )
        db.executemany(
            f"insert or ignore into mined_calls ({_CALL_COLUMNS}) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c.tool_use_id,
                    c.harness,
                    c.session_id,
                    c.at,
                    c.tool,
                    c.tool_kind,
                    c.command,
                    json.dumps(list(c.paths)),
                    c.status,
                    c.error_signature,
                    c.cwd,
                    c.model_id,
                )
                for c in calls
            ],
        )
        db.execute("delete from mined_calls where at < ?", (prune_before,))
        db.execute("commit")
    except BaseException:
        _rollback(db)
        raise


# ---------------------------------------------------------------- CLI reads


def list_lessons(
    db: sqlite3.Connection,
    *,
    statuses: Iterable[Status] | None = None,
    repo_id: RepoId | None = None,
) -> list[Lesson]:
    """Lessons in any of `statuses` (all when None), narrowed to one repo's repo and path scopes."""
    return _load(db, *_list_where(statuses, repo_id))


def _list_where(statuses: Iterable[Status] | None, repo_id: RepoId | None) -> tuple[str, list[object]]:
    where, params = "1", []
    if statuses is not None:
        wanted = sorted(str(s) for s in statuses)
        where += f" and status in ({','.join('?' * len(wanted))})"
        params += wanted
    if repo_id is not None:
        where += " and repo_id = ?"
        params.append(repo_id)
    return where, params


def read_lessons(
    db: sqlite3.Connection,
    *,
    statuses: Iterable[Status] | None = None,
    repo_id: RepoId | None = None,
) -> tuple[list[Lesson], list[tuple[str, str]]]:
    """Like `list_lessons`, but a row that does not load (an empty text, an unknown enum value) comes
    back as `(lesson id, reason)` instead of raising, so one broken row cannot hide the others."""
    where, params = _list_where(statuses, repo_id)
    try:
        return _load(db, where, params), []
    except (ValueError, TypeError):
        pass
    lessons: list[Lesson] = []
    problems: list[tuple[str, str]] = []
    for (lesson_id,) in db.execute(f"select id from lessons where {where} order by id", params).fetchall():
        try:
            lessons.extend(_load(db, "id = ?", (lesson_id,)))
        except (ValueError, TypeError) as exc:
            problems.append((str(lesson_id), str(exc)))
    return lessons, problems


@dataclass(frozen=True)
class EventRow:
    seq: int
    lesson_id: str | None
    type: str
    actor: str
    from_revision: int | None
    to_revision: int | None
    payload: str
    """Canonical JSON, as stored."""
    at: float


def lesson_events(db: sqlite3.Connection, lesson_id: LessonId) -> list[EventRow]:
    """One lesson's events in log order, for `scars why`."""
    rows = db.execute(
        "select seq, lesson_id, type, actor, from_revision, to_revision, payload, at from events "
        "where lesson_id = ? order by seq",
        (lesson_id,),
    )
    return [EventRow(*r) for r in rows]


def observations(db: sqlite3.Connection, lesson_id: LessonId) -> list[Observation]:
    """One lesson's observations, oldest first."""
    rows = db.execute(
        "select lesson_id, harness, session_id, failed_tool_use_id, failed_input, error_signature, "
        "passing_input, at from observations where lesson_id = ? order by at, id",
        (lesson_id,),
    )
    return [Observation(LessonId(r[0]), *r[1:]) for r in rows]
