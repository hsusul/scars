import pytest

from cli_helpers import NOW, SIG, connect, seed
from scars import store, telemetry
from scars.model import EventType, HitOutcome, Status, TrustClass

pytest_plugins = ("cli_fixtures",)


def test_an_edit_may_set_trust_class_human_or_model_and_nothing_else(home):
    lesson_id = seed(home, trust=TrustClass.THIRD_PARTY_REPO)
    db = connect(home)
    with pytest.raises(
        store.Rejected, match="an edit can only set trust class human or model, not 'own_code'"
    ):
        store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.EDITED,
            expected_revision=2,
            actor="human",
            payload={"trust_class": "own_code"},
        )
    store.apply_event(
        db,
        lesson_id=lesson_id,
        event=EventType.EDITED,
        expected_revision=2,
        actor="human",
        payload={"trust_class": "human"},
    )
    assert store.get_lesson(db, lesson_id).trust_class is TrustClass.HUMAN
    assert store.verify(db) == []


def test_list_lessons_filters_by_status_and_repo(home):
    a = seed(home)
    b = seed(home, avoid="npm tset", passing="npm test", sig="npm ERR! tset", provisional=False)
    db = connect(home)
    assert [le.id for le in store.list_lessons(db, statuses=[Status.CANDIDATE])] == [b]
    assert sorted(le.id for le in store.list_lessons(db, repo_id="app")) == sorted([a, b])
    assert store.list_lessons(db, repo_id="other") == []


def test_events_and_observations_of_a_lesson(home):
    lesson_id = seed(home)
    db = connect(home)
    events = store.lesson_events(db, lesson_id)
    assert [(e.type, e.actor, e.from_revision, e.to_revision) for e in events] == [
        ("proposed", "miner", None, 1),
        ("provisionalized", "miner", 1, 2),
    ]
    [obs] = store.observations(db, lesson_id)
    assert (obs.failed_input, obs.passing_input, obs.error_signature) == (
        "pytest --frobnicate tests",
        "pytest tests",
        SIG,
    )


def test_telemetry_reads(home):
    tele = telemetry.connect(str(home / "telemetry.db"))

    def hit(at, lesson, outcome):
        return telemetry.Hit(at, "claude", None, None, "s", None, "tool_failed", "repair", lesson, 1, None,
                             "k", outcome, 0, 1.0, None, None)  # fmt: skip

    hits = (hit(NOW - 100, "L-1", HitOutcome.SHOWN), hit(NOW - 50, "L-2", HitOutcome.SHOWN),
            hit(NOW - 10, None, HitOutcome.NO_MATCH), hit(NOW - 5000, "L-1", HitOutcome.SHOWN))  # fmt: skip
    assert telemetry.write(tele, telemetry.Writes(hits))
    assert [h.at for h in telemetry.hits_since(tele, NOW - 200)] == [NOW - 100, NOW - 50, NOW - 10]
    assert telemetry.last_shown(tele).lesson_id == "L-2"
    assert telemetry.last_shown(tele, "L-1").at == NOW - 100
    assert telemetry.last_shown(tele, "L-9") is None
    assert telemetry.shown_counts(tele, NOW - 200) == {"L-1": 1, "L-2": 1}
