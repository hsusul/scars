"""The read helpers the hook uses on lessons.db."""

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
REPO = RepoId("r-demo")


@pytest.fixture
def db(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    store.add_repo(db, REPO, "demo", "own")
    store.add_repo(db, RepoId("r-other"), "other", "upstream")
    yield db
    db.close()


def new(db, fingerprint):
    return store.create_lesson(
        db,
        text=LessonText("npm test", "run `npm ci`", "Error: missing module"),
        scope=RepoScope(REPO),
        trust_class=TrustClass.OWN_CODE,
        triggers=(Trigger(TriggerKind.ERROR, "Error: missing module", Stage.REPAIR),),
        fingerprint=fingerprint,
        actor="miner",
        at=T0,
    )


def observe(db, lesson_id, tool_use_id, passing):
    db.execute(
        "insert into observations (lesson_id, harness, session_id, failed_tool_use_id, failed_input, "
        "error_signature, passing_input, at) values (?,?,?,?,?,?,?,?)",
        (lesson_id, "claude", "s", tool_use_id, "npm test", "Error: missing module", passing, T0),
    )


def test_candidate_count_counts_only_candidates(db):
    first, _ = new(db, "a"), new(db, "b")
    store.apply_event(
        db, lesson_id=first, event=EventType.PROMOTED, expected_revision=1, actor="human", payload={}
    )
    assert store.candidate_count(db) == 1


def test_repo_for_common_dir_takes_the_first_registered_clone(db):
    db.execute("insert into clones values ('/w/demo/.git', 'r-demo', 1.0)")
    assert store.repo_for_common_dir(db, ["/nowhere/.git", "/w/demo/.git"]) == REPO
    assert store.repo_for_common_dir(db, ["/nowhere/.git"]) is None


def test_repo_names_lists_display_names(db):
    assert store.repo_names(db) == ("demo", "other")


def test_passing_inputs_groups_observations_by_lesson_and_covers_every_id(db):
    a, b = new(db, "a"), new(db, "b")
    observe(db, a, "t1", "npm ci")
    observe(db, a, "t2", "npm ci && npm test")
    observe(db, a, "t3", "npm ci")
    assert store.passing_inputs(db, [a, b, a]) == {
        a: frozenset({"npm ci", "npm ci && npm test"}),
        b: frozenset(),
    }
    assert store.passing_inputs(db, []) == {}


def test_promoted_at_is_the_latest_promotion(db):
    a, b = new(db, "a"), new(db, "b")
    rev = store.apply_event(
        db, lesson_id=a, event=EventType.PROMOTED, expected_revision=1, actor="human", payload={}, at=T0 + 10
    )
    rev = store.apply_event(
        db, lesson_id=a, event=EventType.EDITED, expected_revision=rev, actor="human", payload={}, at=T0 + 20
    )
    store.apply_event(
        db,
        lesson_id=a,
        event=EventType.PROMOTED,
        expected_revision=rev,
        actor="human",
        payload={},
        at=T0 + 30,
    )
    assert store.promoted_at(db, [a, b, LessonId("L-00000000")]) == {a: T0 + 30}
    assert store.promoted_at(db, []) == {}
