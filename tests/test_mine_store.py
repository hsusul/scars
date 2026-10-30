import pytest

from scars import store
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

T0 = 1_800_000_000.0


def call(tool_use_id: str, session_id: str, at: float, command: str | None = "ls") -> store.MinedCall:
    return store.MinedCall(
        tool_use_id, "claude", session_id, at, "Bash", "shell", command, ("a",), "ok", None, "/r", "m"
    )


def pending(tool_use_id: str) -> store.PendingFailure:
    return store.PendingFailure(tool_use_id, "claude", "s", "Bash", "ls", "ls x", "E: x", 3, T0)


def test_commit_batch_replaces_pending_and_prunes_old_calls(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    store.commit_batch(
        db,
        offsets=[store.Offset("/t/a.jsonl", 10, 7, 1.5)],
        seen=["x1", "x2"],
        pending=[pending("p1"), pending("p2")],
        calls=[call("x1", "s", T0), call("x2", "s", T0 + 1), call("old", "s", T0 - 100)],
        prune_before=T0 - 10,
    )
    store.commit_batch(
        db,
        offsets=[store.Offset("/t/a.jsonl", 20, 7, 2.5)],
        seen=["x2", "x3"],
        pending=[pending("p2")],
        calls=[call("x3", "t", T0 + 2)],
        prune_before=T0 - 10,
    )
    assert store.miner_offsets(db) == {"/t/a.jsonl": store.Offset("/t/a.jsonl", 20, 7, 2.5)}
    assert store.seen_tool_uses(db, ["x1", "x3", "nope"]) == frozenset({"x1", "x3"})
    assert store.pending_failures(db) == [pending("p2")]
    assert [c.tool_use_id for c in store.mined_calls_since(db, 0)] == ["x1", "x2", "x3"]
    assert store.session_calls(db, "s") == [call("x1", "s", T0), call("x2", "s", T0 + 1)]
    assert store.mined_call(db, "x3") == call("x3", "t", T0 + 2)
    assert store.mined_call(db, "old") is None


def test_observations_precision_runs_and_clones(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    store.add_repo(db, RepoId("r"), "app", "upstream")
    store.add_clone(db, "/git/a", RepoId("r"), at=T0)
    assert store.repo_for_common_dir(db, ["/git/a"]) == "r"
    assert store.repo_owner_class(db, RepoId("r")) == "upstream"
    assert store.repo_owner_class(db, RepoId("none")) is None
    lesson = store.create_lesson(
        db,
        text=LessonText("ls x", "run `ls` instead", "E: x"),
        scope=RepoScope(RepoId("r")),
        trust_class=TrustClass.THIRD_PARTY_REPO,
        triggers=[Trigger(TriggerKind.ERROR, "E: x", Stage.REPAIR)],
        fingerprint="f",
        actor="miner",
        at=T0,
    )
    obs = store.Observation(LessonId(lesson), "claude", "s", "t1", "ls x", "E: x", "ls", T0)
    assert store.add_observation(db, obs) is True
    assert store.add_observation(db, obs) is False
    assert store.has_observation(db, "t1") is True
    assert store.passing_inputs(db, [lesson]) == {lesson: frozenset({"ls"})}
    store.record_precision(db, lesson, 1, matched_failing=1, matched_passing=2, window_days=30, at=T0)
    store.record_precision(db, lesson, 1, matched_failing=3, matched_passing=0, window_days=30, at=T0 + 1)
    assert db.execute("select * from precision_runs").fetchall() == [(lesson, 1, 3, 0, 30, T0 + 1)]
    assert store.lesson_last_seen(db, lesson) == T0
    assert store.expiring_lessons(db, T0 + store.EXPIRY_SECONDS) == [(lesson, 1)]


def _lesson(db, fingerprint: str) -> LessonId:
    return store.create_lesson(
        db,
        text=LessonText("ls x", "run `ls` instead", "E: x"),
        scope=RepoScope(RepoId("r")),
        trust_class=TrustClass.OWN_CODE,
        triggers=[Trigger(TriggerKind.ERROR, "E: x", Stage.REPAIR)],
        fingerprint=fingerprint,
        actor="miner",
        at=T0,
    )


def test_transaction_commits_its_writes_together_or_not_at_all(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    store.add_repo(db, RepoId("r"), "app", "own")
    with pytest.raises(RuntimeError), store.transaction(db):
        lesson = _lesson(db, "f1")
        store.apply_event(
            db,
            lesson_id=lesson,
            event=EventType.PROVISIONALIZED,
            expected_revision=1,
            actor="miner",
            payload={},
        )
        raise RuntimeError("crash")
    assert db.execute("select count(*) from lessons").fetchone()[0] == 0
    assert db.execute("select count(*) from events").fetchone()[0] == 0
    with store.transaction(db):
        kept = _lesson(db, "f2")
        with pytest.raises(store.Conflict):
            _lesson(db, "f2")
        store.apply_event(
            db,
            lesson_id=kept,
            event=EventType.PROVISIONALIZED,
            expected_revision=1,
            actor="miner",
            payload={},
        )
    assert db.execute("select fingerprint, status, revision from lessons").fetchall() == [
        ("f2", "provisional", 2)
    ]
    assert db.in_transaction is False
    assert store.verify(db) == []


def test_file_contexts_are_saved_with_the_batch(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    ctx = store.FileContext("/t/r.jsonl", "/w/app", "gpt-test")
    store.commit_batch(db, offsets=[], seen=[], pending=[], calls=[], prune_before=0, contexts=[ctx])
    assert store.miner_file_contexts(db) == {"/t/r.jsonl": ctx}


def test_ensure_miner_schema_adds_the_calls_table_to_an_older_store(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    db.execute("drop table mined_calls")
    db.execute("drop table miner_file_context")
    store.ensure_miner_schema(db)
    store.ensure_miner_schema(db)
    assert store.mined_calls_since(db, 0) == []
    assert store.miner_file_contexts(db) == {}
