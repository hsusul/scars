"""`mine --since` leaves skipped sessions alone, and the read paths stay fast on a large store."""

import os
import time

from cli_helpers import DAY, NOW, SIG, claude_transcript, connect, make_ctx, run
from scars import mine, render, store, telemetry
from scars.commands import doctor
from scars.model import HitOutcome, LessonText, RepoScope, Stage, Trigger, TriggerKind, TrustClass

pytest_plugins = ("cli_fixtures",)


def test_mine_since_keeps_pending_failures_of_skipped_transcripts(home, work):
    user = work / "user"
    path = user / ".claude" / "projects" / "p" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        claude_transcript(str(work / "loose"), time.time() - 3600, [("t1", "pytest --frobnicate", SIG)])
    )
    assert run(make_ctx(user, clock=time.time), "mine")[0] == 0
    db = connect(home)
    before = store.pending_failures(db)
    db.close()
    assert [p.tool_use_id for p in before] == ["t1"]
    old = time.time() - 10 * DAY
    os.utime(path, (old, old))
    later = time.time() + 2 * DAY
    out = run(make_ctx(user, clock=lambda: later), "mine", "--since", "1d")[1]
    assert out.startswith("read 0 transcripts: 0 calls, 0 failures, 0 pairs\n")
    db = connect(home)
    assert store.pending_failures(db) == before
    db.close()


def index_names(path):
    db = store.connect(path, readonly=True)
    try:
        return {r[0] for r in db.execute("select name from sqlite_master where type = 'index'")}
    finally:
        db.close()


def test_the_miner_adds_the_read_indexes_to_an_existing_store(home):
    mine.run(home, (), NOW)
    assert {"observations_lesson", "events_lesson", "mined_calls_at"} <= index_names(home / "lessons.db")
    assert {"hits_at", "hits_lesson"} <= index_names(home / "telemetry.db")


def null_model_hit(at, session):
    return telemetry.Hit(
        at, "claude", None, None, session, None, "tool_failed", "repair", None, None, None, None,
        HitOutcome.NO_MATCH, 0, 1.0, None, None,
    )  # fmt: skip


def test_model_backfill_looks_back_only_30_days(home):
    tele = telemetry.connect(str(home / "telemetry.db"))
    assert telemetry.write(
        tele, telemetry.Writes((null_model_hit(NOW - 40 * DAY, "sx"), null_model_hit(NOW - DAY, "sx")))
    )
    tele.close()
    db = connect(home)
    call = store.MinedCall(
        "tu-1", "claude", "sx", NOW - DAY + 5, "Bash", "shell", "ls", (), "ok", None, "/r", "m1"
    )
    store.commit_batch(db, offsets=(), seen=(), pending=(), calls=[call], prune_before=0.0)
    db.close()
    mine.run(home, (), NOW)
    tele = store.connect(home / "telemetry.db", readonly=True)
    assert tele.execute("select at, model_id from hits order by at").fetchall() == [
        (NOW - 40 * DAY, None),
        (NOW - DAY, "m1"),
    ]
    tele.close()


def test_ls_reads_last_seen_with_the_lesson_rows(home, user_home, monkeypatch):
    from cli_helpers import seed

    stale = seed(home, at=NOW - 40 * DAY)
    seed(home, avoid="pytest --fresh", sig="Error: fresh", at=NOW - DAY)

    def per_lesson(*_):
        raise AssertionError("ls asked for one lesson's last_seen_at")

    calls = []
    whole_table = store.lessons_last_seen
    monkeypatch.setattr(store, "lesson_last_seen", per_lesson)
    monkeypatch.setattr(store, "lessons_last_seen", lambda db: calls.append(1) or whole_table(db))
    assert run(make_ctx(user_home), "ls", "--stale") == (
        0,
        "ID          STATUS      LEVEL PIN SEEN  SCOPE  AVOID\n"
        f"{stale:<11} provisional note -      1  repo app  pytest --frobnicate\n",
        "",
    )
    assert calls == [1]


LESSONS = 10_000
HITS = 100_000


def big_store(home, monkeypatch):
    db = connect(home)
    lesson_ids = (f"{i:08x}" for i in range(LESSONS))
    with monkeypatch.context() as patch, store.transaction(db):
        patch.setattr("secrets.token_hex", lambda _: next(lesson_ids))
        for i in range(LESSONS):
            add_lesson(db, i)
    ids = [r[0] for r in db.execute("select id from lessons")]
    db.close()
    tele = telemetry.connect(str(home / "telemetry.db"))
    hits = tuple(
        telemetry.Hit(
            NOW - (n % 45) * DAY,
            "claude",
            None,
            None,
            f"s{n % 500}",
            None,
            "tool_failed",
            "repair",
            ids[n % len(ids)],
            1,
            "candidate",
            "k",
            HitOutcome.SHOWN,
            10,
            2.0,
            None,
            None,
        )
        for n in range(HITS)
    )
    assert telemetry.write(tele, telemetry.Writes(hits))
    tele.close()


def add_lesson(db, i):
    sig = f"tool{i}: error: unknown flag --bad{i}"
    at = NOW - (i % 60) * DAY
    lesson_id = store.create_lesson(
        db,
        text=LessonText(f"tool{i} --bad{i}", f"run `tool{i} --good` instead", render.MINED_REASON),
        scope=RepoScope("app"),
        trust_class=TrustClass.OWN_CODE,
        triggers=[Trigger(TriggerKind.ERROR, sig, Stage.REPAIR)],
        fingerprint=f"fp-{i}",
        actor="miner",
        at=at,
    )
    store.add_observation(
        db,
        store.Observation(
            lesson_id, "claude", f"s{i}", f"tu{i}", f"tool{i} --bad{i}", sig, f"tool{i} --good", at
        ),
    )


def test_ls_and_doctor_on_10000_lessons_and_100000_hits(home, user_home, monkeypatch, capsys):
    started = time.perf_counter()
    big_store(home, monkeypatch)
    built = time.perf_counter() - started
    for name, ensure in (
        ("lessons.db", store.ensure_miner_schema),
        ("telemetry.db", store.ensure_telemetry_schema),
    ):
        db = store.connect(home / name)
        ensure(db)
        db.close()
    monkeypatch.setattr(doctor, "run_canary", lambda: doctor.Check("canary", True, "skipped"))
    timings = {}
    for argv in (["ls"], ["ls", "--stale"], ["doctor"]):
        started = time.perf_counter()
        code, out, _ = run(make_ctx(user_home), *argv)
        timings[" ".join(argv)] = time.perf_counter() - started
        if argv == ["ls"]:
            assert (code, out.count("\n")) == (0, LESSONS + 1)
            assert out.startswith("ID          STATUS      LEVEL PIN SEEN  SCOPE  AVOID\n")
        elif argv == ["ls", "--stale"]:
            # Every lesson's hits fall on days that include one inside the 30-day window.
            assert (code, out) == (0, "no lessons\n")
        else:
            assert code == 1
            assert out.splitlines()[0] == (
                "claude: last hook 2027-01-15 08:00, 4446 runs, p50 2.0 ms, p99 2.0 ms, errors 0 (0.0%), "
                "error classes: none; hooks not installed (run scars init)"
            )
            assert "FAIL  hooks: no harness has scars hooks installed\n" in out
            assert "ok    event chain: hash chain and replay match the lessons\n" in out
            assert "ok    canary: skipped\n" in out
    with capsys.disabled():
        print(f"\nscale: built in {built:.1f} s; " + ", ".join(f"{k} {v:.2f} s" for k, v in timings.items()))
    assert timings["ls"] < 15 and timings["ls --stale"] < 15 and timings["doctor"] < 60
