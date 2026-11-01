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


def staged(db, fingerprint, triggers, steps):
    lesson_id = store.create_lesson(
        db,
        text=LessonText("npm test", "run `npm ci`", "Error: missing module"),
        scope=RepoScope(REPO),
        trust_class=TrustClass.OWN_CODE,
        triggers=triggers,
        fingerprint=fingerprint,
        actor="miner",
        at=T0,
    )
    revision = 1
    for step in steps:
        revision = store.apply_event(
            db, lesson_id=lesson_id, event=step, expected_revision=revision, actor="human", payload={}
        )
    return lesson_id


def test_active_for_stage_keeps_only_lessons_that_can_fire_at_the_stage(db):
    error = Trigger(TriggerKind.ERROR, "Error: missing module", Stage.REPAIR)
    other_error = Trigger(TriggerKind.ERROR, "Error: other", Stage.REPAIR)
    prompt = Trigger(TriggerKind.PROMPT, "express", Stage.PLAN)
    gate = Trigger(TriggerKind.COMMAND, "npm test", Stage.GATE)
    pinned = [EventType.PROMOTED, EventType.PINNED]
    repair = staged(db, "a", (error,), [EventType.PROMOTED])
    other = staged(db, "b", (other_error,), [EventType.PROMOTED])
    staged(db, "f", (error,), [EventType.PROVISIONALIZED])
    both = staged(db, "c", (prompt, other_error), pinned)
    staged(db, "d", (prompt,), [EventType.PROMOTED])
    staged(db, "e", (gate,), pinned[:1])

    def ids(**query):
        return [le.id for le in store.active_for_stage(db, REPO, **query)]

    kinds = {TriggerKind.ERROR}
    assert ids(stage=Stage.REPAIR, pinned_only=False, trigger_kinds=kinds, error_signature=error.spec) == [
        repair
    ]
    assert sorted(ids(stage=Stage.REPAIR, pinned_only=False, trigger_kinds=kinds)) == sorted(
        [repair, other, both]
    )
    plan_kinds = {TriggerKind.PROMPT, TriggerKind.PATH}
    assert ids(stage=Stage.PLAN, pinned_only=True, trigger_kinds=plan_kinds) == [both]
    found = store.active_for_stage(db, REPO, stage=Stage.PLAN, pinned_only=True, trigger_kinds=None)
    assert [(le.id, set(le.triggers)) for le in found] == [(both, {prompt, other_error})]
    assert ids(stage=Stage.PLAN, pinned_only=True, trigger_kinds=plan_kinds) == ids(
        stage=Stage.PLAN, pinned_only=True, trigger_kinds=plan_kinds, error_signature="unused"
    )
    assert store.active_for_stage(db, None, stage=Stage.PLAN, pinned_only=True, trigger_kinds=None) == []


def test_repo_for_common_dir_takes_the_first_registered_clone(db):
    db.execute("insert into clones values ('/w/demo/.git', 'r-demo', 1.0)")
    assert store.repo_for_common_dir(db, ["/nowhere/.git", "/w/demo/.git"]) == REPO
    assert store.repo_for_common_dir(db, ["/nowhere/.git"]) is None


def test_active_for_stage_keeps_raw_and_tool_error_signatures_apart(db):
    tool = staged(
        db,
        "tool",
        (Trigger(TriggerKind.ERROR, "[tool Read] invalid mode", Stage.REPAIR),),
        [EventType.PROMOTED],
    )
    shell = staged(
        db,
        "shell",
        (Trigger(TriggerKind.ERROR, "invalid mode", Stage.REPAIR),),
        [EventType.PROMOTED],
    )

    def ids(raw, qualified=None):
        return sorted(
            lesson.id
            for lesson in store.active_for_stage(
                db,
                REPO,
                stage=Stage.REPAIR,
                pinned_only=False,
                trigger_kinds={TriggerKind.ERROR},
                error_signature=raw,
                tool_error_signature=qualified,
            )
        )

    assert ids("[tool Read] invalid mode") == []
    assert ids("invalid mode") == [shell]
    assert ids("invalid mode", "[tool Read] invalid mode") == sorted([shell, tool])
    assert ids("invalid mode", "[tool Write] invalid mode") == [shell]


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
