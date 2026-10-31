import sqlite3

from scars import store, telemetry
from scars.model import HitOutcome

HIT = telemetry.Hit(
    at=1.0,
    harness="claude",
    harness_version=None,
    model_id=None,
    session_id="s1",
    tool_use_id="tu1",
    event="tool_failed",
    stage="repair",
    lesson_id="L-1",
    lesson_revision=1,
    status_at_injection="promoted",
    trigger_key="k",
    outcome=HitOutcome.SHOWN,
    injected_chars=10,
    latency_ms=1.0,
    error_class=None,
    arm=None,
)
WRITES = telemetry.Writes(
    (HIT,),
    shown=(("L-1", "k"),),
    session_id="s1",
    epoch=1,
    bump_epoch=True,
    cwd_repo=telemetry.CwdRepo("/r", "/r/.git", "r-1", 1.0),
)


def counts(path):
    db = sqlite3.connect(path)
    try:
        return [
            db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("hits", "shown", "session_epoch")
        ]
    finally:
        db.close()


def test_a_missing_garbage_or_truncated_file_is_unavailable(tmp_path):
    store.init_telemetry(tmp_path / "real.db").close()
    (tmp_path / "garbage.db").write_bytes(b"not a database\n" * 300)
    (tmp_path / "truncated.db").write_bytes((tmp_path / "real.db").read_bytes()[:4096])
    names = ("missing.db", "garbage.db", "truncated.db")
    assert [telemetry.connect(str(tmp_path / n)) for n in names] == [None, None, None]


def test_reading_a_file_without_the_tables_gives_the_empty_state(tmp_path):
    sqlite3.connect(tmp_path / "empty.db").execute("create table other (x)").connection.close()
    db = telemetry.connect(str(tmp_path / "empty.db"))
    assert telemetry.read(db, "/r", "s1", compaction=True) == telemetry.EMPTY
    db.close()


def test_a_busy_file_takes_no_writes(tmp_path):
    path = tmp_path / "telemetry.db"
    store.init_telemetry(path).close()
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("begin immediate")
    db = telemetry.connect(str(path))
    try:
        assert telemetry.write(db, WRITES) is False
    finally:
        locker.execute("rollback")
        locker.close()
    assert counts(path) == [0, 0, 0]
    assert telemetry.write(db, WRITES) is True
    assert counts(path) == [1, 1, 1]
    db.close()


def test_a_failing_statement_rolls_back_every_write_and_leaves_the_connection_usable(tmp_path):
    path = tmp_path / "telemetry.db"
    store.init_telemetry(path).close()
    db = telemetry.connect(str(path))
    db.execute("drop table cwd_repo")
    assert telemetry.write(db, WRITES) is False
    assert counts(path) == [0, 0, 0]
    assert telemetry.write(db, telemetry.Writes((HIT,), shown=(("L-1", "k"),), session_id="s1")) is True
    assert counts(path) == [1, 1, 0]
    db.close()
