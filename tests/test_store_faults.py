"""Process-level fault tests from ARCHITECTURE "Fault tests" that target lessons.db."""

import random
import re
import signal
import subprocess
import sys

from scars.model import EventType, LessonText, RepoId, RepoScope, Stage, Trigger, TriggerKind, TrustClass
from scars.store import add_repo, apply_event, connect, create_lesson, get_lesson, init_lessons, verify

RACER = r"""
import sys, time, pathlib
from scars.model import EventType
from scars.store import Conflict, apply_event, connect
path, lesson_id, event, go = sys.argv[1:]
db = connect(pathlib.Path(path))
print("ready", flush=True)
while not pathlib.Path(go).exists():
    time.sleep(0.001)
try:
    apply_event(db, lesson_id=lesson_id, event=EventType(event), expected_revision=1,
                actor="human", payload={})
    print("won", flush=True)
except Conflict:
    print("conflict", flush=True)
"""

WRITER = r"""
import sys, pathlib
from scars.model import EventType
from scars.store import apply_event, connect, get_lesson
path, count, *ids = sys.argv[1:]
db = connect(pathlib.Path(path))
for i in range(int(count)):
    lesson_id = ids[i % len(ids)]
    lesson = get_lesson(db, lesson_id)
    if i % 3 == 0:
        event = EventType.OBSERVED
    else:
        event = EventType.UNPINNED if lesson.pinned else EventType.PINNED
    rev = apply_event(db, lesson_id=lesson_id, event=event, expected_revision=lesson.revision,
                      actor="human", payload={})
    print(f"ok {lesson_id} {rev}", flush=True)
"""

TEXT = LessonText(avoid="a", instead="b", reason="c")


def seeded(tmp_path, count, status=EventType.PROMOTED):
    path = tmp_path / "x y" / "lessons.db"
    db = init_lessons(path)
    add_repo(db, RepoId("ow"), "openwhispr", "own")
    ids = []
    for i in range(count):
        lesson_id = create_lesson(
            db,
            text=TEXT,
            scope=RepoScope(RepoId("ow")),
            trust_class=TrustClass.OWN_CODE,
            triggers=[Trigger(TriggerKind.ERROR, f"sig:{i}", Stage.REPAIR)],
            fingerprint=f"fp{i}",
            actor="miner",
        )
        if status is EventType.PROMOTED:
            apply_event(db, lesson_id=lesson_id, event=status, expected_revision=1, actor="human", payload={})
        ids.append(lesson_id)
    return path, db, ids


def test_two_processes_racing_one_revision_have_exactly_one_winner(tmp_path):
    for round_no in range(5):
        path, db, (lesson_id,) = seeded(tmp_path / str(round_no), 1, status=EventType.PROPOSED)
        go = tmp_path / f"go{round_no}"
        racers = [
            subprocess.Popen(
                [sys.executable, "-c", RACER, str(path), lesson_id, event, str(go)],
                stdout=subprocess.PIPE,
                text=True,
            )
            for event in ("promoted", "retired")
        ]
        assert [p.stdout.readline() for p in racers] == ["ready\n", "ready\n"]
        go.touch()
        outcomes = sorted(p.communicate(timeout=30)[0].strip() for p in racers)
        assert outcomes == ["conflict", "won"]
        lesson = get_lesson(db, lesson_id)
        assert lesson.revision == 2
        assert db.execute("select count(*) from events where lesson_id = ?", (lesson_id,)).fetchone() == (2,)
        assert verify(db) == []


def test_sigkill_during_1000_changes_loses_nothing_committed_and_keeps_the_chain(tmp_path):
    path, db, ids = seeded(tmp_path, 4)
    db.close()
    rng = random.Random(20261029)
    reported: dict[str, int] = {}
    total = kills = 0
    line = re.compile(r"ok (L-[0-9a-f]{8}) (\d+)\n")
    while total < 1000:
        remaining = 1000 - total
        writer = subprocess.Popen(
            [sys.executable, "-c", WRITER, str(path), str(remaining), *ids],
            stdout=subprocess.PIPE,
            text=True,
        )
        kill_after = rng.randint(1, min(150, remaining - 1)) if remaining > 1 else None
        out = ""
        while kill_after is not None and out.count("\n") < kill_after:
            got = writer.stdout.readline()
            if not got:
                break
            out += got
        if kill_after is not None:
            writer.send_signal(signal.SIGKILL)
        out += writer.communicate(timeout=30)[0]
        if writer.returncode == -signal.SIGKILL:
            kills += 1
        for m in line.finditer(out):
            reported[m.group(1)] = max(reported.get(m.group(1), 0), int(m.group(2)))
            total += 1
    assert kills >= 5
    db = connect(path)
    assert db.execute("pragma integrity_check").fetchall() == [("ok",)]
    for lesson_id, rev in reported.items():
        assert get_lesson(db, lesson_id).revision >= rev
        assert db.execute(
            "select count(*) from events where lesson_id = ? and to_revision = ?", (lesson_id, rev)
        ).fetchone() == (1,)
    changes = db.execute("select count(*) from events where type != 'proposed'").fetchone()[0]
    assert changes - len(ids) >= total
    assert verify(db) == []
