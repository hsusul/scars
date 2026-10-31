import sqlite3
from dataclasses import replace

from cli_helpers import first_telemetry, telemetry_version
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


def test_a_first_release_file_is_upgraded_once_by_the_cli_and_then_takes_ignored_hits(tmp_path):
    path = tmp_path / "telemetry.db"
    first_telemetry(path)
    ignored = replace(HIT, outcome=HitOutcome.IGNORED, lesson_id=None, trigger_key=None)
    hook_db = telemetry.connect(str(path))
    assert telemetry.write(hook_db, telemetry.Writes((ignored,))) is False
    hook_db.close()
    assert telemetry_version(path) == 1

    db = store.connect(path, synchronous="NORMAL")
    assert store.ensure_telemetry_schema(db) == 1
    assert store.ensure_telemetry_schema(db) == 2
    db.close()
    hook_db = telemetry.connect(str(path))
    assert telemetry.write(hook_db, telemetry.Writes((ignored,))) is True
    hook_db.close()

    db = sqlite3.connect(path)
    assert db.execute("select seq, harness, outcome from hits order by seq").fetchall() == [
        (7, "codex", "no_match"),
        (8, "claude", "ignored"),
    ]
    assert db.execute(
        "select name from sqlite_master where tbl_name = 'hits' and type = 'index' order by name"
    ).fetchall() == [
        ("hits_at",),
        ("hits_lesson",),
        ("hits_session",),
    ]
    assert db.execute("pragma user_version").fetchone() == (2,)
    db.close()


def test_a_new_file_starts_at_the_current_version(tmp_path):
    store.init_telemetry(tmp_path / "telemetry.db").close()
    assert telemetry_version(tmp_path / "telemetry.db") == 2


def test_a_failed_upgrade_leaves_the_first_release_file_unchanged(tmp_path):
    path = tmp_path / "telemetry.db"
    first_telemetry(path)
    db = store.connect(path, synchronous="NORMAL")
    db.execute("create table hits_old (x)")
    try:
        store.ensure_telemetry_schema(db)
    except sqlite3.OperationalError as exc:
        assert str(exc) == "there is already another table or index with this name: hits_old"
    else:
        raise AssertionError("the upgrade should have failed")
    db.close()
    assert telemetry_version(path) == 1
    db = sqlite3.connect(path)
    assert db.execute("select seq, outcome from hits").fetchall() == [(7, "no_match")]
    db.close()
