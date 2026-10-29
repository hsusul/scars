import pathlib
import re
import sqlite3

import pytest

from scars.store import schema_sql

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md"
LESSON_INSERT = (
    "insert into lessons (id,status,level,pinned,scope_kind,repo_id,avoid,instead,reason,trust_class,"
    "fingerprint,created_at,updated_at,last_seen_at) values (?,?,?,?,?,?,'a','b','c','own_code',?,0,0,0)"
)


def test_architecture_doc_sql_matches_shipped_schema():
    blocks = re.findall(r"```sql\n(.*?)```", DOCS.read_text(), re.S)
    assert blocks == [schema_sql("lessons"), schema_sql("telemetry")]


@pytest.fixture
def lessons():
    db = sqlite3.connect(":memory:")
    db.execute("pragma foreign_keys=on")
    db.executescript(schema_sql("lessons"))
    db.execute("insert into repos values ('ow','openwhispr','own')")
    db.execute(LESSON_INSERT, ("L-1", "provisional", "note", 0, "repo", "ow", "fp1"))
    return db


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (("L-2", "provisional", "block", 0, "repo", "ow", "fp2"), "block level needs promoted status"),
        (("L-3", "candidate", "note", 1, "repo", "ow", "fp3"), "a pin needs promoted status"),
        (("L-4", "provisional", "note", 0, "machine", None, "fp4"), "machine scope is never provisional"),
        (("L-5", "candidate", "note", 0, "repo", "ow", "fp1"), "one active lesson per fingerprint"),
        (("L-6", "bogus", "note", 0, "repo", "ow", "fp6"), "status is an enum"),
    ],
)
def test_lessons_schema_rejects_illegal_rows(lessons, row, why):
    with pytest.raises(sqlite3.IntegrityError):
        lessons.execute(LESSON_INSERT, row)


def test_compare_and_set_update_changes_only_the_expected_revision(lessons):
    first = lessons.execute("update lessons set status='promoted', revision=2 where id='L-1' and revision=1")
    stale = lessons.execute("update lessons set status='retired', revision=2 where id='L-1' and revision=1")
    assert (first.rowcount, stale.rowcount) == (1, 0)
    assert lessons.execute("select status from lessons where id='L-1'").fetchone() == ("promoted",)


def test_hot_path_query_uses_the_active_index(lessons):
    plan = lessons.execute(
        "explain query plan select id from lessons where status in ('provisional','promoted') "
        "and scope_kind='repo' and repo_id='ow'"
    ).fetchall()
    assert any("lessons_active" in row[3] for row in plan)


def test_telemetry_schema_dedupes_by_context_epoch():
    db = sqlite3.connect(":memory:")
    db.executescript(schema_sql("telemetry"))
    db.execute("insert into shown values ('s',0,'L-1','err:x')")
    same_epoch = db.execute("insert or ignore into shown values ('s',0,'L-1','err:x')").rowcount
    next_epoch = db.execute("insert or ignore into shown values ('s',1,'L-1','err:x')").rowcount
    assert (same_epoch, next_epoch) == (0, 1)


def test_telemetry_schema_rejects_delayed_outcomes_at_hook_time():
    db = sqlite3.connect(":memory:")
    db.executescript(schema_sql("telemetry"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "insert into hits (at,harness,session_id,event,outcome) "
            "values (0,'claude','s','pre_tool','heeded')"
        )
