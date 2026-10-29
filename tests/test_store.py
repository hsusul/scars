import sqlite3
import stat

import pytest

import scars.store as store
from scars.model import (
    EventType,
    LessonText,
    Level,
    MachineScope,
    PathScope,
    RepoId,
    RepoScope,
    SessionId,
    SessionScope,
    Stage,
    Status,
    Trigger,
    TriggerKind,
    TrustClass,
)
from scars.store import (
    ACTIVE_FOR_HOOK_WHERE,
    EXPIRY_SECONDS,
    GENESIS_HASH,
    Conflict,
    Rejected,
    active_for_hook,
    add_repo,
    apply_event,
    connect,
    create_lesson,
    get_lesson,
    get_lesson_by_fingerprint,
    inbox_candidates,
    init_lessons,
    init_telemetry,
    next_state,
    verify,
)

OW = RepoId("ow")
TEXT = LessonText(
    avoid="xcodebuild -sdk iphoneos", instead="xcodebuild -sdk iphonesimulator", reason="no device"
)
ERR = Trigger(TriggerKind.ERROR, "sig:abc", Stage.REPAIR)
CMD = Trigger(TriggerKind.COMMAND, "xcodebuild -sdk *", Stage.GATE)
T0 = 1_000_000.0


def db_path(tmp_path):
    return tmp_path / "home dir" / ".scars" / "lessons.db"


@pytest.fixture
def db(tmp_path):
    conn = init_lessons(db_path(tmp_path))
    add_repo(conn, OW, "openwhispr", "own")
    add_repo(conn, RepoId("other"), "other", "upstream")
    yield conn
    conn.close()


def new(db, *, scope=None, fingerprint="fp1", trust=TrustClass.OWN_CODE, triggers=(ERR, CMD), at=T0):
    return create_lesson(
        db,
        text=TEXT,
        scope=scope or RepoScope(OW),
        trust_class=trust,
        triggers=triggers,
        fingerprint=fingerprint,
        actor="miner",
        fix_program="xcodebuild",
        at=at,
    )


def step(db, lesson_id, event, payload=None, at=T0 + 10):
    rev = get_lesson(db, lesson_id).revision
    return apply_event(
        db,
        lesson_id=lesson_id,
        event=event,
        expected_revision=rev,
        actor="human",
        payload=payload or {},
        at=at,
    )


def promoted(db, **kw):
    lesson_id = new(db, **kw)
    step(db, lesson_id, EventType.PROMOTED)
    return lesson_id


def column(db, lesson_id, name):
    return db.execute(f"select {name} from lessons where id = ?", (lesson_id,)).fetchone()[0]


# init and connect


def test_init_creates_private_files_and_schema_version_1(tmp_path):
    path = tmp_path / "a b" / "lessons.db"
    init_lessons(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    db = connect(path)
    assert db.execute("pragma user_version").fetchone() == (1,)
    assert db.execute("pragma journal_mode").fetchone() == ("wal",)
    assert db.execute("pragma foreign_keys").fetchone() == (1,)


def test_init_twice_is_a_no_op_and_keeps_data(db, tmp_path):
    lesson_id = new(db)
    again = init_lessons(tmp_path / "home dir" / ".scars" / "lessons.db")
    assert again.execute("pragma user_version").fetchone() == (1,)
    assert get_lesson(again, lesson_id).id == lesson_id
    assert again.execute("select count(*) from events").fetchone() == (1,)


def test_init_telemetry_applies_the_telemetry_schema(tmp_path):
    path = tmp_path / "t" / "telemetry.db"
    db = init_telemetry(path)
    db.execute("insert into session_epoch (session_id) values ('s')")
    assert db.execute("select context_epoch from session_epoch").fetchone() == (0,)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    db.close()
    init_telemetry(path).close()


def test_lessons_connection_uses_full_sync_and_given_busy_timeout(db, tmp_path):
    conn = connect(tmp_path / "home dir" / ".scars" / "lessons.db", busy_timeout_ms=200)
    assert conn.execute("pragma synchronous").fetchone() == (2,)
    assert conn.execute("pragma busy_timeout").fetchone() == (200,)


def test_readonly_connect_reads_but_cannot_write(db, tmp_path):
    lesson_id = new(db)
    ro = connect(tmp_path / "home dir" / ".scars" / "lessons.db", readonly=True, busy_timeout_ms=200)
    assert get_lesson(ro, lesson_id).status is Status.CANDIDATE
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("update lessons set sessions_seen = 9")
    assert ro.execute("pragma busy_timeout").fetchone() == (200,)


def test_readonly_connect_does_not_create_a_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "missing dir" / "lessons.db", readonly=True)
    assert not (tmp_path / "missing dir").exists()


# create and read


def test_create_lesson_stores_a_candidate_with_triggers_and_a_proposed_event(db):
    lesson_id = new(db)
    lesson = get_lesson(db, lesson_id)
    assert lesson_id.startswith("L-") and len(lesson_id) == 10
    int(lesson_id[2:], 16)
    assert (lesson.status, lesson.revision, lesson.level, lesson.pinned) == (
        Status.CANDIDATE,
        1,
        Level.NOTE,
        False,
    )
    assert lesson.scope == RepoScope(OW)
    assert lesson.text == TEXT
    assert lesson.trust_class is TrustClass.OWN_CODE
    assert set(lesson.triggers) == {ERR, CMD}
    assert (lesson.fingerprint, lesson.sessions_seen) == ("fp1", 1)
    assert column(db, lesson_id, "expires_at") == T0 + EXPIRY_SECONDS
    assert column(db, lesson_id, "fix_programs") == '["xcodebuild"]'
    rows = db.execute(
        "select lesson_id, type, actor, from_revision, to_revision, prev_hash from events"
    ).fetchall()
    assert rows == [(lesson_id, "proposed", "miner", None, 1, GENESIS_HASH)]


@pytest.mark.parametrize(
    ("scope", "columns"),
    [
        (RepoScope(OW), ("repo", "ow", None, None)),
        (PathScope(OW, "src/**/*.swift"), ("path", "ow", "src/**/*.swift", None)),
        (SessionScope(SessionId("s1")), ("session", None, None, "s1")),
    ],
)
def test_scope_round_trips_through_its_columns(db, scope, columns):
    lesson_id = new(db, scope=scope)
    assert (
        db.execute(
            "select scope_kind, repo_id, path_glob, session_id from lessons where id = ?", (lesson_id,)
        ).fetchone()
        == columns
    )
    assert get_lesson(db, lesson_id).scope == scope


def test_create_lesson_rejects_machine_scope(db):
    with pytest.raises(Rejected):
        new(db, scope=MachineScope())
    assert db.execute("select count(*) from lessons").fetchone() == (0,)


def test_create_lesson_with_an_active_fingerprint_conflicts_and_writes_nothing(db):
    new(db)
    with pytest.raises(Conflict):
        new(db)
    assert db.execute("select count(*) from events").fetchone() == (1,)


def test_a_retired_fingerprint_can_be_proposed_again(db):
    old = new(db)
    step(db, old, EventType.RETIRED)
    successor = new(db)
    assert get_lesson_by_fingerprint(db, "fp1").id == successor


def test_get_lesson_returns_none_for_an_unknown_id(db):
    assert get_lesson(db, "L-00000000") is None


def test_active_for_hook_returns_only_lessons_visible_to_this_repo_and_session(db):
    repo_promoted = promoted(db, fingerprint="a")
    path_prov = new(db, fingerprint="b", scope=PathScope(OW, "*.py"))
    step(db, path_prov, EventType.PROVISIONALIZED)
    machine = new(db, fingerprint="c")
    step(db, machine, EventType.PROMOTED, {"scope": {"kind": "machine"}})
    mine = new(db, fingerprint="d", scope=SessionScope(SessionId("s1")))
    new(db, fingerprint="e", scope=SessionScope(SessionId("s2")))
    new(db, fingerprint="f")
    promoted(db, fingerprint="g", scope=RepoScope(RepoId("other")))
    retired = promoted(db, fingerprint="h")
    step(db, retired, EventType.RETIRED)

    got = {lesson.id for lesson in active_for_hook(db, OW, SessionId("s1"))}
    assert got == {repo_promoted, path_prov, machine, mine}
    outside = {lesson.id for lesson in active_for_hook(db, None, SessionId("s9"))}
    assert outside == {machine}


def test_active_for_hook_query_uses_the_active_index(db):
    plan = db.execute(
        f"explain query plan select * from lessons where {ACTIVE_FOR_HOOK_WHERE}", ("ow", "s")
    ).fetchall()
    searches = [row[3] for row in plan if row[3].startswith(("SEARCH", "SCAN"))]
    assert searches and all("USING INDEX lessons_active" in s for s in searches)


def test_inbox_candidates_lists_candidates_only(db):
    a = new(db, fingerprint="a")
    b = new(db, fingerprint="b", scope=SessionScope(SessionId("s1")))
    promoted(db, fingerprint="c")
    assert {lesson.id for lesson in inbox_candidates(db)} == {a, b}


# lifecycle


def test_apply_event_bumps_revision_and_appends_one_chained_event(db):
    lesson_id = new(db)
    assert (
        apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PROMOTED,
            expected_revision=1,
            actor="human",
            payload={},
            at=5.0,
        )
        == 2
    )
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.revision) == (Status.PROMOTED, 2)
    assert column(db, lesson_id, "expires_at") is None
    assert column(db, lesson_id, "updated_at") == 5.0
    events = db.execute(
        "select type, from_revision, to_revision, payload from events order by seq"
    ).fetchall()
    assert events == [("proposed", None, 1, events[0][3]), ("promoted", 1, 2, "{}")]
    assert verify(db) == []


def test_stale_revision_raises_conflict_and_changes_nothing(db):
    lesson_id = new(db)
    step(db, lesson_id, EventType.PROVISIONALIZED)
    with pytest.raises(Conflict):
        apply_event(
            db, lesson_id=lesson_id, event=EventType.PROMOTED, expected_revision=1, actor="human", payload={}
        )
    assert get_lesson(db, lesson_id).status is Status.PROVISIONAL
    assert db.execute("select count(*) from events").fetchone() == (2,)
    assert not db.in_transaction


def test_observed_counts_a_sighting_and_resets_expiry(db):
    lesson_id = new(db)
    step(db, lesson_id, EventType.OBSERVED, {"fix_program": "xcrun"}, at=T0 + 100)
    step(db, lesson_id, EventType.OBSERVED, {"fix_program": "xcrun"}, at=T0 + 200)
    assert get_lesson(db, lesson_id).sessions_seen == 3
    assert column(db, lesson_id, "last_seen_at") == T0 + 200
    assert column(db, lesson_id, "expires_at") == T0 + 200 + EXPIRY_SECONDS
    assert column(db, lesson_id, "fix_programs") == '["xcodebuild","xcrun"]'


def test_observed_on_a_promoted_lesson_keeps_no_expiry(db):
    lesson_id = promoted(db)
    step(db, lesson_id, EventType.OBSERVED, at=T0 + 100)
    assert (get_lesson(db, lesson_id).status, column(db, lesson_id, "expires_at")) == (Status.PROMOTED, None)


def test_pin_and_unpin_toggle_the_pin(db):
    lesson_id = promoted(db)
    step(db, lesson_id, EventType.PINNED)
    assert get_lesson(db, lesson_id).pinned is True
    step(db, lesson_id, EventType.UNPINNED)
    assert get_lesson(db, lesson_id).pinned is False


def test_edited_returns_a_lesson_to_candidate_and_clears_pin_level_and_precision(db):
    lesson_id = promoted(db)
    step(db, lesson_id, EventType.PINNED)
    step(db, lesson_id, EventType.LEVEL_SET, {"level": "warn"})
    db.execute("update lessons set precision_ok = 1 where id = ?", (lesson_id,))
    new_text = {"avoid": "a", "instead": "b", "reason": "c"}
    new_triggers = [{"kind": "error", "spec": "sig:zzz", "stage": "repair"}]
    step(db, lesson_id, EventType.EDITED, {"text": new_text, "triggers": new_triggers}, at=T0 + 50)
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.pinned, lesson.level) == (Status.CANDIDATE, False, Level.NOTE)
    assert lesson.text == LessonText("a", "b", "c")
    assert lesson.triggers == (Trigger(TriggerKind.ERROR, "sig:zzz", Stage.REPAIR),)
    assert column(db, lesson_id, "precision_ok") == 0
    assert column(db, lesson_id, "expires_at") == T0 + EXPIRY_SECONDS
    assert verify(db) == []


def test_edited_keeps_a_machine_lesson_promoted_with_pin_level_and_precision_cleared(db):
    lesson_id = new(db)
    step(db, lesson_id, EventType.PROMOTED, {"scope": {"kind": "machine"}})
    step(db, lesson_id, EventType.PINNED)
    step(db, lesson_id, EventType.LEVEL_SET, {"level": "warn"})
    db.execute("update lessons set precision_ok = 1 where id = ?", (lesson_id,))
    step(db, lesson_id, EventType.EDITED, {"text": {"avoid": "a", "instead": "b", "reason": "c"}})
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.pinned, lesson.level, lesson.scope) == (
        Status.PROMOTED,
        False,
        Level.NOTE,
        MachineScope(),
    )
    assert column(db, lesson_id, "precision_ok") == 0
    assert column(db, lesson_id, "expires_at") is None
    assert verify(db) == []


def test_promoting_to_machine_scope_clears_the_repo_columns(db):
    lesson_id = new(db)
    step(db, lesson_id, EventType.PROMOTED, {"scope": {"kind": "machine"}})
    assert db.execute(
        "select scope_kind, repo_id, path_glob, session_id from lessons where id = ?", (lesson_id,)
    ).fetchone() == ("machine", None, None, None)


def test_promoting_into_session_scope_is_rejected(db):
    lesson_id = new(db)
    with pytest.raises(Rejected):
        step(db, lesson_id, EventType.PROMOTED, {"scope": {"kind": "session", "session_id": "s"}})


def scope_columns(db, lesson_id):
    return db.execute(
        "select scope_kind, repo_id, path_glob, session_id from lessons where id = ?", (lesson_id,)
    ).fetchone()


@pytest.mark.parametrize("prior", [[], [EventType.PROVISIONALIZED]])
def test_promoting_a_session_lesson_without_a_new_scope_is_rejected(db, prior):
    lesson_id = new(db, scope=SessionScope(SessionId("s1")))
    for event in prior:
        step(db, lesson_id, event)
    before = get_lesson(db, lesson_id)
    with pytest.raises(Rejected, match="session"):
        step(db, lesson_id, EventType.PROMOTED)
    assert get_lesson(db, lesson_id) == before


def test_promoting_a_session_lesson_with_repo_scope_makes_it_visible_to_the_repo(db):
    lesson_id = new(db, scope=SessionScope(SessionId("s1")))
    step(db, lesson_id, EventType.PROMOTED, {"scope": {"kind": "repo", "repo_id": "ow"}})
    assert scope_columns(db, lesson_id) == ("repo", "ow", None, None)
    assert [lesson.id for lesson in active_for_hook(db, OW, SessionId("s2"))] == [lesson_id]
    assert verify(db) == []


PROPOSAL = {
    "text": {"avoid": "a", "instead": "b", "reason": "c"},
    "scope": {"kind": "session", "session_id": "s1"},
    "trust_class": "own_code",
    "triggers": [],
    "fingerprint": "f",
}


def test_replay_rejects_promoting_a_session_lesson_without_a_scope():
    with pytest.raises(Rejected, match="session"):
        next_state(next_state(None, EventType.PROPOSED, PROPOSAL, at=T0), EventType.PROMOTED, {}, at=T0)


def test_replay_applies_an_edited_scope():
    edited = next_state(
        next_state(None, EventType.PROPOSED, PROPOSAL, at=T0),
        EventType.EDITED,
        {"scope": {"kind": "path", "repo_id": "ow", "glob": "src/**"}},
        at=T0 + 5,
    )
    assert (edited.scope, edited.created_at, edited.updated_at) == (PathScope(OW, "src/**"), T0, T0 + 5)


@pytest.mark.parametrize(
    ("start", "payload", "scope", "columns"),
    [
        (
            RepoScope(OW),
            {"kind": "path", "repo_id": "ow", "glob": "src/**"},
            PathScope(OW, "src/**"),
            ("path", "ow", "src/**", None),
        ),
        (PathScope(OW, "*.py"), {"kind": "repo", "repo_id": "ow"}, RepoScope(OW), ("repo", "ow", None, None)),
        (
            SessionScope(SessionId("s1")),
            {"kind": "repo", "repo_id": "other"},
            RepoScope(RepoId("other")),
            ("repo", "other", None, None),
        ),
        (
            RepoScope(OW),
            {"kind": "session", "session_id": "s2"},
            SessionScope(SessionId("s2")),
            ("session", None, None, "s2"),
        ),
    ],
)
def test_edited_scope_moves_the_lesson_back_to_candidate_and_clears_unused_columns(
    db, start, payload, scope, columns
):
    lesson_id = new(db, scope=start)
    if start.kind != "session":
        step(db, lesson_id, EventType.PROMOTED)
        step(db, lesson_id, EventType.PINNED)
    step(db, lesson_id, EventType.EDITED, {"scope": payload})
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.pinned, lesson.scope) == (Status.CANDIDATE, False, scope)
    assert scope_columns(db, lesson_id) == columns
    assert verify(db) == []


def test_editing_a_machine_lesson_into_repo_scope_returns_it_to_candidate(db):
    lesson_id = new(db)
    step(db, lesson_id, EventType.PROMOTED, {"scope": {"kind": "machine"}})
    step(db, lesson_id, EventType.EDITED, {"scope": {"kind": "repo", "repo_id": "ow"}}, at=T0 + 20)
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.scope) == (Status.CANDIDATE, RepoScope(OW))
    assert scope_columns(db, lesson_id) == ("repo", "ow", None, None)
    assert column(db, lesson_id, "expires_at") == T0 + EXPIRY_SECONDS
    assert verify(db) == []


def test_level_set_note_and_warn_need_no_precision_run(db):
    lesson_id = promoted(db)
    step(db, lesson_id, EventType.LEVEL_SET, {"level": "warn"})
    assert get_lesson(db, lesson_id).level is Level.WARN


def precision_run(db, lesson_id, failing, passing):
    rev = get_lesson(db, lesson_id).revision
    db.execute("insert into precision_runs values (?,?,?,?,30,0)", (lesson_id, rev, failing, passing))


@pytest.mark.parametrize(("failing", "passing"), [(2, 0), (100, 2), (3, 1)])
def test_level_block_is_rejected_when_the_precision_run_fails_the_block_rule(db, failing, passing):
    lesson_id = promoted(db)
    precision_run(db, lesson_id, failing, passing)
    with pytest.raises(Rejected, match="block rule"):
        step(db, lesson_id, EventType.LEVEL_SET, {"level": "block"})
    assert get_lesson(db, lesson_id).level is Level.NOTE


@pytest.mark.parametrize(("failing", "passing"), [(3, 0), (199, 1)])
def test_level_block_passes_with_a_precision_run_for_this_revision(db, failing, passing):
    lesson_id = promoted(db)
    precision_run(db, lesson_id, failing, passing)
    step(db, lesson_id, EventType.LEVEL_SET, {"level": "block"})
    assert get_lesson(db, lesson_id).level is Level.BLOCK


def test_level_block_ignores_a_precision_run_for_an_older_revision(db):
    lesson_id = promoted(db)
    precision_run(db, lesson_id, 10, 0)
    step(db, lesson_id, EventType.PINNED)
    with pytest.raises(Rejected, match="precision run for revision 3"):
        step(db, lesson_id, EventType.LEVEL_SET, {"level": "block"})


@pytest.mark.parametrize("trust", [TrustClass.WEB, TrustClass.THIRD_PARTY_REPO])
def test_level_block_is_rejected_for_untrusted_sources(db, trust):
    lesson_id = promoted(db, trust=trust)
    precision_run(db, lesson_id, 10, 0)
    with pytest.raises(Rejected, match="never reach level block"):
        step(db, lesson_id, EventType.LEVEL_SET, {"level": "block"})


def test_superseded_records_the_successor(db):
    old = promoted(db, fingerprint="a")
    successor = new(db, fingerprint="b")
    step(db, old, EventType.SUPERSEDED, {"superseded_by": successor})
    assert (get_lesson(db, old).status, column(db, old, "superseded_by")) == (Status.SUPERSEDED, successor)


@pytest.mark.parametrize("payload", [{}, {"superseded_by": "L-ffffffff"}, {"superseded_by": "SELF"}])
def test_superseded_without_a_real_other_successor_is_rejected(db, payload):
    old = promoted(db)
    if payload.get("superseded_by") == "SELF":
        payload = {"superseded_by": old}
    with pytest.raises(Rejected):
        step(db, old, EventType.SUPERSEDED, payload)
    assert get_lesson(db, old).status is Status.PROMOTED


def test_retiring_a_pinned_warn_lesson_clears_pin_and_level(db):
    lesson_id = promoted(db)
    step(db, lesson_id, EventType.PINNED)
    step(db, lesson_id, EventType.LEVEL_SET, {"level": "warn"})
    step(db, lesson_id, EventType.MUTED)
    lesson = get_lesson(db, lesson_id)
    assert (lesson.status, lesson.pinned, lesson.level) == (Status.RETIRED, False, Level.NOTE)
    assert verify(db) == []


@pytest.mark.parametrize(
    ("setup", "event", "payload"),
    [
        ([], EventType.PINNED, {}),
        ([], EventType.UNPINNED, {}),
        ([], EventType.LEVEL_SET, {"level": "warn"}),
        ([EventType.PROMOTED], EventType.PROVISIONALIZED, {}),
        ([EventType.PROMOTED], EventType.EXPIRED, {}),
        ([EventType.PROMOTED], EventType.LEVEL_SET, {"level": "loud"}),
        ([EventType.PROVISIONALIZED], EventType.PROVISIONALIZED, {}),
        ([EventType.RETIRED], EventType.OBSERVED, {}),
        ([EventType.RETIRED], EventType.PROMOTED, {}),
        ([EventType.RETIRED], EventType.EDITED, {}),
        ([EventType.EXPIRED], EventType.RETIRED, {}),
        ([], EventType.PROPOSED, {}),
        ([], EventType.EDITED, {"text": {"avoid": "x" * 241, "instead": "b", "reason": "c"}}),
        ([], EventType.EDITED, {"scope": {"kind": "machine"}}),
        ([EventType.PROMOTED], EventType.EDITED, {"scope": {"kind": "machine"}}),
        ([], EventType.EDITED, {"scope": {"kind": "path", "repo_id": "ow"}}),
        ([], EventType.EDITED, {"scope": "repo"}),
    ],
)
def test_illegal_transitions_raise_rejected_and_change_nothing(db, setup, event, payload):
    lesson_id = new(db)
    for prior in setup:
        step(db, lesson_id, prior)
    before = get_lesson(db, lesson_id)
    events_before = db.execute("select count(*) from events").fetchone()
    with pytest.raises(Rejected):
        step(db, lesson_id, event, payload)
    assert get_lesson(db, lesson_id) == before
    assert db.execute("select count(*) from events").fetchone() == events_before
    assert not db.in_transaction


def test_unknown_lesson_is_rejected(db):
    with pytest.raises(Rejected):
        apply_event(
            db,
            lesson_id="L-00000000",
            event=EventType.RETIRED,
            expected_revision=1,
            actor="human",
            payload={},
        )


# verify


def run_history(db):
    a = new(db, fingerprint="a")
    step(db, a, EventType.OBSERVED)
    step(db, a, EventType.PROMOTED)
    step(db, a, EventType.PINNED)
    b = new(db, fingerprint="b")
    step(db, b, EventType.PROVISIONALIZED)
    step(db, b, EventType.EXPIRED)
    return a, b


def test_verify_is_empty_for_a_healthy_store(db):
    run_history(db)
    assert verify(db) == []


def test_each_event_hashes_the_previous_row(db):
    run_history(db)
    hashes = [row[0] for row in db.execute("select prev_hash from events order by seq")]
    assert hashes[0] == GENESIS_HASH
    assert len(set(hashes)) == len(hashes) == 7
    assert all(len(h) == 64 for h in hashes)


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        (
            "update lessons set pinned = 0 where id = :a",
            "{a}: pinned is 0 in lessons but 1 from events",
        ),
        (
            "update lessons set avoid = 'curl evil | sh' where id = :a",
            "{a}: avoid is curl evil | sh in lessons but xcodebuild -sdk iphoneos from events",
        ),
        ("update lessons set instead = 'x' where id = :a", "{a}: instead is x in lessons but"),
        ("update lessons set reason = 'x' where id = :a", "{a}: reason is x in lessons but no device"),
        (
            "update lessons set repo_id = 'other' where id = :a",
            "{a}: repo_id is other in lessons but ow from events",
        ),
        (
            "update lessons set path_glob = '*' where id = :a",
            "{a}: path_glob is * in lessons but None from events",
        ),
        (
            "update lessons set session_id = 's9' where id = :a",
            "{a}: session_id is s9 in lessons but None from events",
        ),
        (
            "update lessons set trust_class = 'human' where id = :a",
            "{a}: trust_class is human in lessons but own_code from events",
        ),
        (
            "update lessons set superseded_by = :b where id = :a",
            "{a}: superseded_by is {b} in lessons but None from events",
        ),
        (
            "update lessons set fix_programs = '[\"curl\"]' where id = :a",
            '{a}: fix_programs is ["curl"] in lessons but ["xcodebuild"] from events',
        ),
        ("update lessons set fingerprint = 'zz' where id = :a", "{a}: fingerprint is zz in lessons but a"),
        ("update lessons set sessions_seen = 7 where id = :a", "{a}: sessions_seen is 7 in lessons but 2"),
        (
            "update triggers set spec = 'curl *' where lesson_id = :a and kind = 'command'",
            "{a}: triggers are ",
        ),
        ("delete from triggers where lesson_id = :a and kind = 'error'", "{a}: triggers are "),
        (
            "insert into triggers values (:a, 'prompt', 'deploy', 'plan')",
            "{a}: triggers are ",
        ),
        ("update lessons set status = 'candidate', pinned = 0 where id = :a", "{a}: status is candidate"),
        ("update lessons set revision = 9 where id = :b", "{b}: revision is 9 in lessons but 3 from events"),
        (
            "update lessons set level = 'warn' where id = :a",
            "{a}: level is warn in lessons but note from events",
        ),
    ],
)
def test_verify_reports_a_lesson_row_edited_behind_the_stores_back(db, sql, problem):
    a, b = run_history(db)
    db.execute(sql, {"a": a, "b": b})
    problems = verify(db)
    assert problems
    assert any(p.startswith(problem.format(a=a, b=b)) for p in problems), problems


def test_verify_reports_a_broken_prev_hash(db):
    run_history(db)
    db.execute("update events set prev_hash = ? where seq = 3", ("f" * 64,))
    assert verify(db) == [
        "event 3: prev_hash does not extend the chain",
        "event 4: prev_hash does not extend the chain",
    ]


def test_verify_reports_an_edited_event_payload_through_the_next_hash(db):
    run_history(db)
    db.execute("update events set actor = 'human' where seq = 1")
    assert verify(db) == ["event 2: prev_hash does not extend the chain"]


def test_verify_reports_a_deleted_event(db):
    a, _ = run_history(db)
    db.execute("delete from events where seq = 3")
    problems = verify(db)
    assert problems[0] == "event 4: prev_hash does not extend the chain"
    assert problems[1].startswith(f"event 4: {a} cannot replay pinned")


def test_verify_reports_a_lesson_inserted_without_events(db):
    db.execute(
        "insert into lessons (id,status,scope_kind,repo_id,avoid,instead,reason,trust_class,fingerprint,"
        "created_at,updated_at,last_seen_at) values ('L-deadbeef','promoted','repo','ow','a','b','c',"
        "'own_code','x',0,0,0)"
    )
    assert verify(db) == ["L-deadbeef: has no events"]


def test_verify_reports_events_whose_lessons_row_was_deleted(db):
    _, b = run_history(db)
    db.execute("pragma foreign_keys = off")
    db.execute("delete from triggers where lesson_id = ?", (b,))
    db.execute("delete from lessons where id = ?", (b,))
    assert verify(db) == [f"{b}: has events but no lessons row"]


def test_verify_reports_a_revision_gap_in_the_events(db):
    _, b = run_history(db)
    db.execute("update events set to_revision = 5 where seq = 7")
    assert verify(db) == [f"event 7: {b} revisions 2->5, expected 2->3"]


def test_verify_reports_an_out_of_band_scope_change(db):
    a, _ = run_history(db)
    db.execute("update lessons set scope_kind = 'path', path_glob = 'src/**' where id = ?", (a,))
    assert verify(db) == [
        f"{a}: scope_kind is path in lessons but repo from events",
        f"{a}: path_glob is src/** in lessons but None from events",
    ]


def timed_history(db):
    """Four lessons whose timestamps come from observed, edited, promoted and expired events."""
    a = new(db, fingerprint="a", at=T0)
    step(db, a, EventType.OBSERVED, at=T0 + 100)
    step(db, a, EventType.EDITED, {"text": {"avoid": "a", "instead": "b", "reason": "c"}}, at=T0 + 200)
    step(db, a, EventType.PROMOTED, at=T0 + 300)
    b = new(db, fingerprint="b", at=T0 + 1)
    step(db, b, EventType.OBSERVED, at=T0 + 50)
    step(db, b, EventType.PROVISIONALIZED, at=T0 + 60)
    step(db, b, EventType.EXPIRED, at=T0 + 70)
    c = new(db, fingerprint="c", at=T0 + 2)
    step(db, c, EventType.OBSERVED, at=T0 + 400)
    step(db, c, EventType.EDITED, {}, at=T0 + 500)
    d = new(db, fingerprint="d", at=T0 + 3)
    step(db, d, EventType.PROMOTED, at=T0 + 600)
    step(db, d, EventType.EDITED, {}, at=T0 + 700)
    return {"a": a, "b": b, "c": c, "d": d}


def times(db, lesson_id):
    return db.execute(
        "select status, created_at, updated_at, last_seen_at, expires_at from lessons where id = ?",
        (lesson_id,),
    ).fetchone()


def test_timestamps_follow_the_events_and_verify_agrees(db):
    ids = timed_history(db)
    assert times(db, ids["a"]) == ("promoted", 1000000.0, 1000300.0, 1000100.0, None)
    assert times(db, ids["b"]) == ("retired", 1000001.0, 1000070.0, 1000050.0, None)
    assert times(db, ids["c"]) == ("candidate", 1000002.0, 1000500.0, 1000400.0, 2210000.0)
    # An edit returns a promoted lesson to candidate, expiring 14 days after its last sighting.
    assert times(db, ids["d"]) == ("candidate", 1000003.0, 1000700.0, 1000003.0, 2209603.0)
    assert verify(db) == []


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        (
            "update lessons set expires_at = null where id = :c",
            "{c}: expires_at is None in lessons but 2210000.0",
        ),
        (
            "update lessons set expires_at = expires_at + 86400 where id = :c",
            "{c}: expires_at is 2296400.0 in lessons but 2210000.0",
        ),
        (
            "update lessons set expires_at = null where id = :d",
            "{d}: expires_at is None in lessons but 2209603.0",
        ),
        (
            "update lessons set expires_at = 5e6 where id = :a",
            "{a}: expires_at is 5000000.0 in lessons but None",
        ),
        (
            "update lessons set expires_at = 5e6 where id = :b",
            "{b}: expires_at is 5000000.0 in lessons but None",
        ),
        (
            "update lessons set created_at = 0 where id = :a",
            "{a}: created_at is 0.0 in lessons but 1000000.0",
        ),
        (
            "update lessons set created_at = 0 where id = :c",
            "{c}: created_at is 0.0 in lessons but 1000002.0",
        ),
        (
            "update lessons set updated_at = 0 where id = :a",
            "{a}: updated_at is 0.0 in lessons but 1000300.0",
        ),
        (
            "update lessons set updated_at = 0 where id = :b",
            "{b}: updated_at is 0.0 in lessons but 1000070.0",
        ),
        (
            "update lessons set updated_at = 0 where id = :c",
            "{c}: updated_at is 0.0 in lessons but 1000500.0",
        ),
        (
            "update lessons set updated_at = 0 where id = :d",
            "{d}: updated_at is 0.0 in lessons but 1000700.0",
        ),
        (
            "update lessons set last_seen_at = 0 where id = :a",
            "{a}: last_seen_at is 0.0 in lessons but 1000100.0",
        ),
        (
            "update lessons set last_seen_at = 0 where id = :b",
            "{b}: last_seen_at is 0.0 in lessons but 1000050.0",
        ),
        (
            "update lessons set last_seen_at = 0 where id = :c",
            "{c}: last_seen_at is 0.0 in lessons but 1000400.0",
        ),
        (
            "update lessons set last_seen_at = 0 where id = :d",
            "{d}: last_seen_at is 0.0 in lessons but 1000003.0",
        ),
    ],
)
def test_verify_reports_a_tampered_timestamp(db, sql, problem):
    ids = timed_history(db)
    db.execute(sql, ids)
    assert verify(db) == [problem.format(**ids) + " from events"]


def test_verify_reports_a_payload_edited_to_another_valid_shape_through_the_next_hash(db):
    run_history(db)
    # The edit replays to the same state, so only the hash chain can see it.
    db.execute("""update events set payload = '{"fix_program":"xcodebuild"}' where seq = 2""")
    assert verify(db) == ["event 3: prev_hash does not extend the chain"]


@pytest.mark.parametrize(
    ("seq", "sql", "reason"),
    [
        (1, "update events set payload = 'null' where seq = 1", "payload must be an object, got None"),
        (1, "update events set payload = '[1,2]' where seq = 1", "payload must be an object, got [1, 2]"),
        (1, "update events set payload = '5' where seq = 1", "payload must be an object, got 5"),
        (1, """update events set payload = '"x"' where seq = 1""", "payload must be an object, got 'x'"),
        (
            1,
            "update events set payload = json_remove(payload, '$.fingerprint') where seq = 1",
            "payload is missing fingerprint",
        ),
        (
            1,
            "update events set payload = json_set(payload, '$.fingerprint', 5) where seq = 1",
            "payload fingerprint must be a string, got 5",
        ),
        (
            1,
            "update events set payload = json_set(payload, '$.text', 'x') where seq = 1",
            "payload text must be an object, got 'x'",
        ),
        (
            1,
            "update events set payload = json_set(payload, '$.triggers', json('{}')) where seq = 1",
            "payload triggers must be a list, got {}",
        ),
        (
            1,
            "update events set payload = json_set(payload, '$.text.avoid', json('[1]')) where seq = 1",
            "text avoid must be a string, got [1]",
        ),
        (
            1,
            "update events set payload = json_set(payload, '$.triggers[0].spec', 7) where seq = 1",
            "invalid trigger {'kind': 'error', 'spec': 7, 'stage': 'repair'}",
        ),
        (
            3,
            "update events set payload = json_set(payload, '$.fix_program', 5) where seq = 3",
            "payload fix_program must be a string, got 5",
        ),
        (
            3,
            "update events set payload = json_set(payload, '$.bogus', 1) where seq = 3",
            "payload has unknown key 'bogus'",
        ),
        (3, "update events set payload = X'ff' where seq = 3", "payload must be JSON text, got b'\\xff'"),
        (
            3,
            "update events set payload = 'nope' where seq = 3",
            "payload is not JSON: Expecting value: line 1 column 1 (char 0)",
        ),
        (3, "update events set at = 'later' where seq = 3", "at must be a number, got 'later'"),
    ],
)
def test_verify_reports_a_malformed_event_and_keeps_verifying_other_lessons(db, seq, sql, reason):
    a = new(db, fingerprint="a")
    b = new(db, fingerprint="b")
    step(db, a, EventType.OBSERVED, {"fix_program": "xcrun"})
    step(db, b, EventType.OBSERVED)
    db.execute(sql)
    db.execute("update lessons set sessions_seen = 9 where id = ?", (b,))
    kind = "proposed" if seq == 1 else "observed"
    assert verify(db) == [
        f"event {seq}: {a} cannot replay {kind}: {reason}",
        f"event {seq + 1}: prev_hash does not extend the chain",
        f"{b}: sessions_seen is 9 in lessons but 2 from events",
    ]


def test_verify_reports_a_non_integer_revision_without_raising(db):
    a = new(db, fingerprint="a")
    b = new(db, fingerprint="b")
    step(db, a, EventType.OBSERVED)
    db.execute("update events set to_revision = 'two' where seq = 3")
    db.execute("update lessons set sessions_seen = 9 where id = ?", (b,))
    assert verify(db) == [
        f"event 3: {a} revisions 1->two, expected 1->2",
        f"{b}: sessions_seen is 9 in lessons but 1 from events",
    ]


def two_lessons_with_b_tampered(db):
    a = new(db, fingerprint="a")
    b = new(db, fingerprint="b")
    step(db, a, EventType.OBSERVED, {"fix_program": "xcrun"})
    step(db, b, EventType.OBSERVED)
    db.execute("update lessons set sessions_seen = 9 where id = ?", (b,))
    # A tamperer is not bound by the schema's checks.
    db.execute("pragma ignore_check_constraints = on")
    db.execute("pragma foreign_keys = off")
    return a, b


def test_verify_reports_a_payload_nested_too_deeply_and_keeps_verifying_other_lessons(db):
    a, b = two_lessons_with_b_tampered(db)
    db.execute("update events set payload = ? where seq = 1", ("[" * 10000 + "0" + "]" * 10000,))
    assert verify(db) == [
        f"event 1: {a} cannot replay proposed: payload nests too deeply",
        "event 2: prev_hash does not extend the chain",
        f"{b}: sessions_seen is 9 in lessons but 2 from events",
    ]


@pytest.mark.parametrize(
    ("table", "column", "rowid", "chain"),
    [
        ("events", "payload", 3, ["event 4: prev_hash does not extend the chain"]),
        ("events", "type", 3, ["event 4: prev_hash does not extend the chain"]),
        ("events", "actor", 3, ["event 4: prev_hash does not extend the chain"]),
        ("events", "at", 3, ["event 4: prev_hash does not extend the chain"]),
        (
            "events",
            "prev_hash",
            3,
            ["event 3: prev_hash does not extend the chain", "event 4: prev_hash does not extend the chain"],
        ),
        ("events", "payload", 1, ["event 2: prev_hash does not extend the chain"]),
        ("lessons", "avoid", 1, []),
        ("lessons", "status", 1, []),
        ("lessons", "fix_programs", 1, []),
    ],
)
def test_verify_reports_invalid_utf8_text_and_keeps_verifying_other_lessons(db, table, column, rowid, chain):
    _, b = two_lessons_with_b_tampered(db)
    db.execute(f"update {table} set {column} = cast(X'FF' as text) where rowid = ?", (rowid,))
    assert verify(db) == [
        f"{table} row {rowid}: {column} is not valid UTF-8",
        *chain,
        f"{b}: sessions_seen is 9 in lessons but 2 from events",
    ]
    assert db.text_factory is str


@pytest.mark.parametrize("column", ["spec", "kind", "stage", "lesson_id"])
def test_verify_reports_an_invalid_utf8_trigger_and_keeps_verifying_other_lessons(db, column):
    a, b = two_lessons_with_b_tampered(db)
    # a's triggers are rows 1 (command) and 2 (error), written in sorted order.
    db.execute(f"update triggers set {column} = cast(X'FF' as text) where rowid = 2")
    by_lesson = {
        a: f"{a}: triggers are [('command', 'xcodebuild -sdk *', 'gate')] in lessons but "
        "[('command', 'xcodebuild -sdk *', 'gate'), ('error', 'sig:abc', 'repair')] from events",
        b: f"{b}: sessions_seen is 9 in lessons but 2 from events",
    }
    assert verify(db) == [
        f"triggers row 2: {column} is not valid UTF-8",
        *(by_lesson[lesson_id] for lesson_id in sorted(by_lesson)),
    ]


def test_verify_reports_an_invalid_utf8_lesson_id_in_events_and_keeps_verifying(db):
    a, b = two_lessons_with_b_tampered(db)
    db.execute("update events set lesson_id = cast(X'FF' as text) where seq = 1")
    assert verify(db) == [
        "events row 1: lesson_id is not valid UTF-8",
        "event 2: prev_hash does not extend the chain",
        f"event 3: {a} cannot replay observed: observed needs an existing lesson",
        f"{b}: sessions_seen is 9 in lessons but 2 from events",
        "<invalid UTF-8 ff>: has events but no lessons row",
    ]


def test_verify_reports_an_invalid_utf8_lesson_id_in_lessons_and_keeps_verifying(db):
    a, b = two_lessons_with_b_tampered(db)
    db.execute("update lessons set id = cast(X'FF' as text) where id = ?", (a,))
    assert verify(db) == [
        "lessons row 1: id is not valid UTF-8",
        f"{b}: sessions_seen is 9 in lessons but 2 from events",
        "<invalid UTF-8 ff>: has no events",
        f"{a}: has events but no lessons row",
    ]


def test_verify_accepts_a_chain_with_non_ascii_text(db):
    text = LessonText(avoid="rm -rf ~/Übung", instead="trash 日本語/", reason="keeps files 🗂")
    lesson_id = create_lesson(
        db,
        text=text,
        scope=RepoScope(OW),
        trust_class=TrustClass.OWN_CODE,
        triggers=[Trigger(TriggerKind.COMMAND, "rm -rf ~/Ü*", Stage.GATE)],
        fingerprint="fp-é",
        actor="miner",
        at=T0,
    )
    step(db, lesson_id, EventType.OBSERVED, {"fix_program": "trash-ü"})
    new(db, fingerprint="after")
    assert verify(db) == []


def test_verify_never_hashes_invalid_utf8_like_the_text_it_replaced(db):
    # Byte FF read as latin-1 is the text it replaced, so the hash must keep invalid bytes distinct.
    create_lesson(
        db,
        text=TEXT,
        scope=RepoScope(OW),
        trust_class=TrustClass.OWN_CODE,
        triggers=[ERR],
        fingerprint="a",
        actor="ÿ",
        at=T0,
    )
    new(db, fingerprint="b")
    db.execute("update events set actor = cast(X'FF' as text) where seq = 1")
    assert verify(db) == [
        "events row 1: actor is not valid UTF-8",
        "event 2: prev_hash does not extend the chain",
    ]


def test_verify_restores_the_callers_text_factory(db):
    two_lessons_with_b_tampered(db)
    db.text_factory = bytes
    assert len(verify(db)) == 1
    assert db.text_factory is bytes


@pytest.mark.parametrize(
    ("event", "payload", "reason"),
    [
        (EventType.OBSERVED, {"bogus": 1}, "payload has unknown key 'bogus'"),
        (EventType.OBSERVED, {"fix_program": 5}, "payload fix_program must be a string, got 5"),
        (EventType.LEVEL_SET, {}, "payload is missing level"),
        (EventType.SUPERSEDED, {"superseded_by": 3}, "payload superseded_by must be a string, got 3"),
    ],
)
def test_apply_event_rejects_a_payload_replay_would_reject(db, event, payload, reason):
    lesson_id = promoted(db)
    with pytest.raises(Rejected) as exc:
        step(db, lesson_id, event, payload)
    assert str(exc.value) == reason
    assert get_lesson(db, lesson_id).revision == 2


# read snapshots


def commit_between_reads(monkeypatch, reader, write):
    """Run `write` on another connection right after `reader`'s first query in the store returns."""
    real = store._rows
    fired = []

    def hooked(conn, sql, params=()):
        result = real(conn, sql, params)
        if conn is reader and not fired:
            fired.append(sql)
            write()
        return result

    monkeypatch.setattr(store, "_rows", hooked)
    return fired


LOADERS = {
    "active_for_hook": lambda conn, lesson_id: active_for_hook(conn, OW, SessionId("s1")),
    "get_lesson": lambda conn, lesson_id: [get_lesson(conn, lesson_id)],
    "get_lesson_by_fingerprint": lambda conn, lesson_id: [get_lesson_by_fingerprint(conn, "fp1")],
    "inbox_candidates": lambda conn, lesson_id: inbox_candidates(conn),
}


@pytest.mark.parametrize("loader", list(LOADERS))
def test_loaders_never_pair_a_lesson_row_with_triggers_from_a_later_edit(db, tmp_path, monkeypatch, loader):
    lesson_id = new(db, scope=SessionScope(SessionId("s1")))
    reader = connect(db_path(tmp_path))
    before = get_lesson(reader, lesson_id)
    edit = {
        "text": {"avoid": "rm -rf build", "instead": "make clean", "reason": "keeps cache"},
        "triggers": [{"kind": "prompt", "spec": "clean", "stage": "plan"}],
    }
    fired = commit_between_reads(monkeypatch, reader, lambda: step(db, lesson_id, EventType.EDITED, edit))
    got = LOADERS[loader](reader, lesson_id)
    assert fired, "the concurrent edit never ran"
    assert got == [before]
    assert not reader.in_transaction
    assert LOADERS[loader](reader, lesson_id)[0].triggers == (
        Trigger(TriggerKind.PROMPT, "clean", Stage.PLAN),
    )


def test_loader_inside_a_caller_transaction_leaves_it_open(db, tmp_path):
    lesson_id = new(db)
    reader = connect(db_path(tmp_path))
    reader.execute("begin")
    assert get_lesson(reader, lesson_id).id == lesson_id
    assert reader.in_transaction
    reader.execute("commit")


def test_verify_reads_events_and_lessons_from_one_snapshot(db, tmp_path, monkeypatch):
    a, _ = run_history(db)
    reader = connect(db_path(tmp_path))

    def write():
        step(db, a, EventType.UNPINNED)
        new(db, fingerprint="late")

    fired = commit_between_reads(monkeypatch, reader, write)
    assert verify(reader) == []
    assert fired, "the concurrent write never ran"
    assert not reader.in_transaction
    assert verify(reader) == []
