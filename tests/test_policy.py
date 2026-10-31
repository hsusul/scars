import pytest

from scars import policy, store
from scars.match import Precision
from scars.model import (
    EventType,
    LessonText,
    RepoId,
    RepoScope,
    Stage,
    Trigger,
    TriggerKind,
    TrustClass,
)

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.mark.parametrize(
    ("failing", "passing", "kept"), [(1, 4, True), (1, 5, False), (0, 0, True), (0, 1, False)]
)
def test_pre_trigger_is_kept_at_four_passing_per_failing(failing, passing, kept):
    assert policy.keeps_pre_trigger(Precision(failing, passing, failing + passing, 100)) is kept


def _lesson(db, fingerprint: str) -> str:
    store.add_repo(db, RepoId("r"), "app", "own")
    return store.create_lesson(
        db,
        text=LessonText("pytest --x", "run `pytest` instead", "E: x"),
        scope=RepoScope(RepoId("r")),
        trust_class=TrustClass.OWN_CODE,
        triggers=[Trigger(TriggerKind.ERROR, "E: x", Stage.REPAIR)],
        fingerprint=fingerprint,
        actor="miner",
        at=T0,
    )


def test_expiry_retires_candidates_and_provisionals_after_fourteen_days(tmp_path):
    db = store.init_lessons(tmp_path / "lessons.db")
    candidate = _lesson(db, "a")
    provisional = _lesson(db, "b")
    store.apply_event(
        db,
        lesson_id=provisional,
        event=EventType.PROVISIONALIZED,
        expected_revision=1,
        actor="miner",
        payload={},
        at=T0,
    )
    promoted = _lesson(db, "c")
    store.apply_event(
        db,
        lesson_id=promoted,
        event=EventType.PROMOTED,
        expected_revision=1,
        actor="human",
        payload={},
        at=T0,
    )
    assert policy.expire(db, T0 + 14 * DAY - 1) == 0
    assert policy.expire(db, T0 + 14 * DAY) == 2
    statuses = dict(db.execute("select id, status from lessons").fetchall())
    assert statuses == {candidate: "retired", provisional: "retired", promoted: "promoted"}
    assert policy.expire(db, T0 + 30 * DAY) == 0
    assert store.verify(db) == []
