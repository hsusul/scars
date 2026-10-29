"""Execute the SQL in docs/ARCHITECTURE.md and assert the invariants the design relies on.

Usage: python3 check_schema.py [path/to/ARCHITECTURE.md]
"""

import pathlib
import re
import sqlite3
import sys

doc = pathlib.Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else pathlib.Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md"
)
blocks = re.findall(r"```sql\n(.*?)```", doc.read_text(), re.S)
assert len(blocks) == 2, f"expected 2 sql blocks (lessons.db, telemetry.db), found {len(blocks)}"

for ddl in blocks:
    sqlite3.connect(":memory:").executescript(ddl)

db = sqlite3.connect(":memory:")
db.execute("pragma foreign_keys=on")
db.executescript(blocks[0])
db.execute("insert into repos values ('ow','openwhispr','own')")
insert = (
    "insert into lessons (id,status,level,pinned,scope_kind,repo_id,avoid,instead,reason,trust_class,fingerprint,"
    "created_at,updated_at,last_seen_at) values (?,?,?,?,?,?,'a','b','c','own_code',?,0,0,0)"
)
db.execute(insert, ("L-1", "provisional", "note", 0, "repo", "ow", "fp1"))

must_reject = {
    "block level on a provisional lesson": ("L-2", "provisional", "block", 0, "repo", "ow", "fp2"),
    "pin on a candidate": ("L-3", "candidate", "note", 1, "repo", "ow", "fp3"),
    "machine-scope provisional lesson": ("L-4", "provisional", "note", 0, "machine", None, "fp4"),
    "second active lesson with the same fingerprint": ("L-5", "candidate", "note", 0, "repo", "ow", "fp1"),
    "unknown status": ("L-6", "bogus", "note", 0, "repo", "ow", "fp6"),
}
failures = []
for label, row in must_reject.items():
    try:
        db.execute(insert, row)
        failures.append(f"accepted {label}")
    except sqlite3.IntegrityError:
        pass

first = db.execute(
    "update lessons set status='promoted', revision=revision+1 where id='L-1' and revision=1"
).rowcount
stale = db.execute(
    "update lessons set status='retired', revision=revision+1 where id='L-1' and revision=1"
).rowcount
if (first, stale) != (1, 0):
    failures.append(f"compare-and-set gave {(first, stale)}, expected (1, 0)")

plan = db.execute(
    "explain query plan select id from lessons where status in ('provisional','promoted') "
    "and scope_kind='repo' and repo_id='ow'"
).fetchall()
if not any("lessons_active" in row[3] for row in plan):
    failures.append(f"hot query does not use lessons_active: {plan}")

db.execute(
    "insert into observations (lesson_id,harness,session_id,failed_tool_use_id,failed_input,error_signature,passing_input,at) "
    "values ('L-1','claude','s1','tu-1','cd apps/x','no such file','cd x',0)"
)
try:
    db.execute(
        "insert into observations (lesson_id,harness,session_id,failed_tool_use_id,failed_input,error_signature,passing_input,at) "
        "values ('L-1','claude','s2','tu-1','cd apps/x','no such file','cd x',0)"
    )
    failures.append("observations accepted the same failed tool_use_id twice")
except sqlite3.IntegrityError:
    pass
db.execute(
    "insert into pending_failures (tool_use_id,harness,session_id,tool,program,input,error_signature,last_call_at) "
    "values ('tu-9','codex','s3','Bash','tox','tox -e py','spaces in path',0)"
)
if (
    db.execute("update pending_failures set calls_since = calls_since + 1 where tool_use_id='tu-9'").rowcount
    != 1
):
    failures.append("pending_failures could not track look-ahead progress")

tele = sqlite3.connect(":memory:")
tele.executescript(blocks[1])
try:
    tele.execute(
        "insert into hits (at,harness,session_id,event,outcome) values (0,'claude','s','pre_tool','heeded')"
    )
    failures.append("hits accepted a delayed outcome at hook time")
except sqlite3.IntegrityError:
    pass
tele.execute(
    "insert into hits (at,harness,session_id,event,outcome) values (0,'claude','s','session_start','shown')"
)
tele.execute("insert into outcomes values (1,'no_attempt')")
try:
    tele.execute("insert into outcomes values (2,'maybe')")
    failures.append("outcomes accepted an unknown outcome")
except sqlite3.IntegrityError:
    pass
tele.execute("insert into shown values ('s',0,'L-1','err:x')")
dup = tele.execute("insert or ignore into shown values ('s',0,'L-1','err:x')").rowcount
again = tele.execute("insert or ignore into shown values ('s',1,'L-1','err:x')").rowcount
if (dup, again) != (0, 1):
    failures.append(f"shown dedupe by epoch gave {(dup, again)}, expected (0, 1)")

print("schema checks:", "PASS" if not failures else "FAIL")
for f in failures:
    print("FAIL", f)
sys.exit(1 if failures else 0)
