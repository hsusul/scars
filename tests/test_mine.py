import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scars import mine, store
from scars.mine import Source, Summary
from scars.model import Harness, RepoId, RepoScope, Stage, Status, Trigger, TriggerKind, TrustClass

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
START = 1_800_000_000.0
DAY = 86400.0
FAIL_TEXT = "pytest: error: unrecognized arguments: --frobnicate"
SIG = "pytest: error: unrecognized arguments: --frobnicate"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


class Claude:
    """Builds a synthetic Claude Code transcript, one tool call at a time."""

    def __init__(self, cwd: str, start: float = START) -> None:
        self.cwd = cwd
        self.at = start
        self.records: list[dict[str, object]] = []

    def call(self, tool_id: str, name: str, args: dict[str, object], error: str | None = None) -> "Claude":
        self.at += 10
        self.records.append(
            {
                "type": "assistant",
                "timestamp": iso(self.at),
                "cwd": self.cwd,
                "message": {
                    "model": "claude-test-1",
                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
                },
            }
        )
        self.records.append(
            {
                "type": "user",
                "timestamp": iso(self.at + 1),
                "cwd": self.cwd,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "is_error": error is not None,
                            "content": error if error is not None else "fine",
                        }
                    ]
                },
            }
        )
        return self

    def bash(self, tool_id: str, command: str, error: str | None = None) -> "Claude":
        return self.call(tool_id, "Bash", {"command": command}, error)

    def read(self, tool_id: str) -> "Claude":
        return self.call(tool_id, "Read", {"file_path": f"{self.cwd}/README.md"})

    def edit(self, tool_id: str) -> "Claude":
        return self.call(
            tool_id, "Edit", {"file_path": f"{self.cwd}/a.py", "old_string": "a", "new_string": "b"}
        )

    def text(self) -> str:
        return "".join(json.dumps(r) + "\n" for r in self.records)


@pytest.fixture
def tmp_root():
    """A temp dir with no long random path segment. The secret scan's entropy rule flags the random
    segment of macOS's default temp dir, and the miner would then store no working directory."""
    root = Path(tempfile.mkdtemp(prefix="scars-mine-", dir="/tmp")).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def env(tmp_root: Path) -> dict[str, object]:
    tmp_path = tmp_root
    home = tmp_path / "home"
    projects = tmp_path / "projects"
    repo = tmp_path / "work" / "app"
    upstream = tmp_path / "work" / "lib"
    for d in (projects / "p", repo, upstream, tmp_path / "work" / "loose"):
        d.mkdir(parents=True)
    db = store.init_lessons(home / "lessons.db")
    store.add_repo(db, RepoId("own-app"), "app", "own")
    store.add_repo(db, RepoId("up-lib"), "lib", "upstream")
    store.add_clone(db, "/git/app", RepoId("own-app"))
    store.add_clone(db, "/git/lib", RepoId("up-lib"))
    db.close()
    return {
        "home": home,
        "projects": projects,
        "repo": str(repo),
        "upstream": str(upstream),
        "loose": str(tmp_path / "work" / "loose"),
    }


def common_dir_of(cwd: str) -> str | None:
    if cwd.endswith("/app") or "/app/" in cwd:
        return "/git/app"
    if cwd.endswith("/lib") or "/lib/" in cwd:
        return "/git/lib"
    return None


def mine_now(
    env: dict[str, object], now: float = START + DAY, sources: list[Source] | None = None
) -> Summary:
    sources = sources or [Source(Harness.CLAUDE, env["projects"], "*/*.jsonl")]  # type: ignore[arg-type]
    return mine.run(env["home"], sources, now, common_dir_of=common_dir_of)  # type: ignore[arg-type]


def write(env: dict[str, object], name: str, text: str) -> Path:
    path = env["projects"] / "p" / f"{name}.jsonl"  # type: ignore[operator]
    path.write_text(text)
    return path


def lessons_db(env: dict[str, object]) -> sqlite3.Connection:
    return store.connect(env["home"] / "lessons.db")  # type: ignore[operator]


def basic_pair(cwd: str, start: float = START, prefix: str = "t") -> Claude:
    return (
        Claude(cwd, start)
        .bash(f"{prefix}1", "pytest --frobnicate tests", FAIL_TEXT)
        .read(f"{prefix}2")
        .bash(f"{prefix}3", "pytest tests")
    )


def test_pair_in_own_repo_is_an_own_code_candidate_with_both_triggers(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert summary == Summary(
        sessions_read=1, calls=3, failures=1, pairs=1, candidates_created=1, observations=1
    )
    db = lessons_db(env)
    (lesson,) = store.inbox_candidates(db)
    assert lesson.status is Status.CANDIDATE
    assert lesson.trust_class is TrustClass.OWN_CODE
    assert (lesson.text.avoid, lesson.text.instead, lesson.text.reason) == (
        "pytest --frobnicate tests",
        "run `pytest tests` instead",
        MINED_WHY,
    )
    assert set(lesson.triggers) == {
        Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR),
        Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.GATE),
    }
    assert db.execute("select failed_input, passing_input, session_id from observations").fetchall() == [
        ("pytest --frobnicate tests", "pytest tests", "s1")
    ]
    assert db.execute(
        "select revision, matched_failing, matched_passing, window_days from precision_runs"
    ).fetchall() == [(1, 1, 0, 30)]
    assert db.execute("select expires_at from lessons").fetchone()[0] == START + 31 + 14 * DAY
    assert store.verify(db) == []


FIXTURE_CWD = "/tmp/scars-fixture/app"


def copy_fixture(source: Path, target: Path, repo: str) -> None:
    """Copy a static transcript with its working directory moved into this test's own repo dir."""
    target.write_text(source.read_text().replace(FIXTURE_CWD, repo))


def test_static_fixture_pairs_like_the_builder(env):
    copy_fixture(FIXTURES / "claude" / "pair.jsonl", env["projects"] / "p" / "fixture.jsonl", env["repo"])
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created) == (1, 1)


def test_edit_between_failure_and_fix_is_no_pair(env):
    t = (
        Claude(env["repo"])
        .bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
        .edit("t2")
        .bash("t3", "pytest tests")
    )
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.failures, summary.pairs, summary.candidates_created) == (1, 0, 0)


def test_fix_with_a_different_program_is_no_pair(env):
    t = (
        Claude(env["repo"])
        .bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
        .bash("t2", "python3 -m unittest")
    )
    write(env, "s1", t.text())
    assert mine_now(env) == Summary(sessions_read=1, calls=2, failures=1)
    db = lessons_db(env)
    assert db.execute("select tool_use_id, calls_since from pending_failures").fetchall() == [("t1", 1)]
    assert db.execute("select count(*) from lessons").fetchone()[0] == 0


@pytest.mark.parametrize(("between", "pairs"), [(9, 1), (10, 0)])
def test_fix_must_come_within_ten_calls(env, between, pairs):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    for i in range(between):
        t.read(f"r{i}")
    t.bash("fix", "pytest tests")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.calls, summary.failures, summary.pairs, summary.candidates_created) == (
        between + 2,
        1,
        pairs,
        pairs,
    )
    db = lessons_db(env)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0


def test_copied_history_is_ignored(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    assert mine_now(env).observations == 1
    resumed = basic_pair(env["repo"])
    resumed.read("new1")
    write(env, "s2", resumed.text())
    summary = mine_now(env)
    assert (summary.calls, summary.failures, summary.pairs, summary.observations) == (1, 0, 0, 0)


def test_truncated_trailing_line_waits_for_its_newline(env):
    full = basic_pair(env["repo"]).text()
    path = write(env, "s1", full[:-1])
    first = mine_now(env)
    assert (first.calls, first.failures, first.pairs) == (2, 1, 0)
    db = lessons_db(env)
    assert db.execute("select tool_use_id, calls_since from pending_failures").fetchall() == [("t1", 1)]
    # The offset stops before the call whose result line is incomplete, so the next batch rereads it.
    tail = "".join(full.splitlines(keepends=True)[-2:])
    assert db.execute("select offset from miner_offsets").fetchone()[0] == len(full.encode()) - len(
        tail.encode()
    )
    path.write_text(full)
    second = mine_now(env)
    assert (second.calls, second.pairs, second.candidates_created) == (1, 1, 1)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0


def test_rotated_file_is_read_from_the_start(env):
    old = Claude(env["repo"])
    for i in range(6):
        old.read(f"a{i}")
    path = write(env, "s1", old.text())
    assert mine_now(env).calls == 6
    os.remove(path)
    write(env, "s1", basic_pair(env["repo"], prefix="b").text())
    summary = mine_now(env)
    assert (summary.calls, summary.pairs) == (3, 1)


def test_replaced_file_with_a_new_inode_is_read_from_the_start(env):
    path = write(env, "s1", Claude(env["repo"]).read("a1").text())
    assert mine_now(env).calls == 1
    longer = basic_pair(env["repo"], prefix="b")
    for i in range(3):
        longer.read(f"x{i}")
    staged = path.with_suffix(".tmp")
    staged.write_text(longer.text())
    os.replace(staged, path)
    assert mine_now(env).calls == 6


def test_file_truncated_in_place_is_read_from_the_start(env):
    old = Claude(env["repo"])
    for i in range(6):
        old.read(f"a{i}")
    path = write(env, "s1", old.text())
    inode = path.stat().st_ino
    assert mine_now(env).calls == 6
    with path.open("w") as f:
        f.write(Claude(env["repo"]).read("b1").text())
    assert path.stat().st_ino == inode
    assert mine_now(env).calls == 1


def test_crash_before_the_batch_commit_does_not_double_count(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    mine_now(env)
    db = lessons_db(env)
    db.execute("delete from miner_offsets")
    db.execute("delete from seen_tool_use")
    summary = mine_now(env)
    assert (summary.pairs, summary.observations, summary.candidates_created) == (1, 0, 0)
    assert db.execute("select sessions_seen from lessons").fetchall() == [(1,)]


def test_pending_failure_completes_in_the_next_batch(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT).read("t2")
    path = write(env, "s1", t.text())
    assert mine_now(env).pairs == 0
    t.records = []
    t.bash("t3", "pytest tests")
    with path.open("a") as f:
        f.write(t.text())
    summary = mine_now(env)
    assert (summary.calls, summary.pairs, summary.candidates_created) == (1, 1, 1)


def test_pending_failure_is_dropped_after_a_day_idle(env):
    write(env, "s1", Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT).text())
    mine_now(env, now=START + 3600)
    db = lessons_db(env)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 1
    mine_now(env, now=START + 11 + DAY - 1)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 1
    mine_now(env, now=START + 11 + DAY)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0


def test_upstream_repo_pair_stays_a_third_party_candidate(env):
    write(env, "s1", basic_pair(env["upstream"]).text())
    summary = mine_now(env)
    assert summary.candidates_created == 1
    (lesson,) = store.inbox_candidates(lessons_db(env))
    assert (lesson.status, lesson.trust_class, lesson.scope.kind) == (
        Status.CANDIDATE,
        TrustClass.THIRD_PARTY_REPO,
        "repo",
    )


def test_pair_outside_registered_repos_is_a_session_candidate(env):
    write(env, "s1", basic_pair(env["loose"]).text())
    summary = mine_now(env)
    assert summary.candidates_created == 1
    (lesson,) = store.inbox_candidates(lessons_db(env))
    assert (lesson.status, lesson.scope.kind) == (Status.CANDIDATE, "session")


def test_candidate_with_a_secret_is_dropped_and_counted(env):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    t = (
        Claude(env["repo"])
        .bash("t1", "deploy --token=bad", "deploy: invalid token for target")
        .bash("t2", f"deploy --token={token}")
    )
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created) == (1, 0)
    assert summary.rejected == {"secret.github_token": 1}
    assert lessons_db(env).execute("select count(*) from lessons").fetchone()[0] == 0


def test_secret_in_the_failed_input_alone_is_dropped(env):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    t = (
        Claude(env["repo"])
        .bash("t1", f"pytest --frobnicate tests --token={token}", FAIL_TEXT)
        .bash("t2", f"pytest tests --token={token}")
    )
    write(env, "s1", t.text())
    assert mine_now(env).rejected == {"secret.github_token": 1}


def test_a_secret_in_the_failed_command_drops_the_candidate(env):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    t = (
        Claude(env["repo"])
        .bash("t1", f"pytest --frobnicate tests --auth {token}", FAIL_TEXT)
        .bash("t2", "pytest tests")
    )
    write(env, "s1", t.text())
    assert mine_now(env).rejected == {"secret.github_token": 1}


def test_lessons_expire_fourteen_days_after_the_last_sighting(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    last_seen = START + 31
    assert mine_now(env, now=last_seen + 14 * DAY - 1).expired == 0
    summary = mine_now(env, now=last_seen + 14 * DAY)
    assert summary.expired == 1
    db = lessons_db(env)
    assert db.execute("select status from lessons").fetchall() == [("retired",)]
    assert store.verify(db) == []


def test_second_sighting_observes_the_same_lesson_and_resets_expiry(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    write(env, "s2", basic_pair(env["repo"], start=START + 5 * DAY, prefix="u").text())
    summary = mine_now(env, now=START + 6 * DAY)
    assert (summary.pairs, summary.candidates_created, summary.observations) == (2, 1, 2)
    db = lessons_db(env)
    assert db.execute("select status, sessions_seen, last_seen_at, fix_programs from lessons").fetchall() == [
        ("candidate", 2, START + 5 * DAY + 31, '["pytest"]')
    ]
    assert store.verify(db) == []


def test_a_mined_lesson_that_no_longer_renders_is_retired_and_mined_afresh(env):
    from scars.mine.build import fingerprint
    from scars.model import LessonText

    db = lessons_db(env)
    legacy = store.create_lesson(
        db,
        text=LessonText("pytest --frobnicate", "run `pytest tests` instead", FAIL_TEXT),
        scope=RepoScope(RepoId("own-app")),
        trust_class=TrustClass.OWN_CODE,
        triggers=(Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR),),
        fingerprint=fingerprint(RepoScope(RepoId("own-app")), SIG),
        actor="miner",
        at=START,
    )
    store.add_observation(
        db,
        store.Observation(
            legacy, "claude", "s0", "old1", "pytest --frobnicate tests", SIG, "pytest tests", START
        ),
    )
    db.commit()
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.retired_unrenderable) == (1, {"mined.reason_template": 1})
    assert db.execute(
        "select type, payload from events where lesson_id = ? order by seq", (legacy,)
    ).fetchall() == [
        (
            "proposed",
            db.execute(
                "select payload from events where lesson_id = ? and type = 'proposed'", (legacy,)
            ).fetchone()[0],
        ),
        ("retired", '{"reason":"cannot render: mined.reason_template"}'),
    ]
    (fresh,) = store.inbox_candidates(db)
    assert (fresh.text.avoid, fresh.text.reason, fresh.sessions_seen) == (
        "pytest --frobnicate tests",
        MINED_WHY,
        1,
    )
    assert store.verify(db) == []


def legacy_lesson(env, fingerprint="fp-legacy", reason=FAIL_TEXT):
    from scars.model import LessonText

    db = lessons_db(env)
    legacy = store.create_lesson(
        db,
        text=LessonText("pytest --frobnicate tests", "run `pytest tests` instead", reason),
        scope=RepoScope(RepoId("own-app")),
        trust_class=TrustClass.OWN_CODE,
        triggers=(Trigger(TriggerKind.ERROR, "pytest: error: old signature", Stage.REPAIR),),
        fingerprint=fingerprint,
        actor="miner",
        at=START,
    )
    store.add_observation(
        db,
        store.Observation(
            legacy, "claude", "s0", "old1", "pytest --frobnicate tests", SIG, "pytest tests", START
        ),
    )
    db.commit()
    return db, legacy


def test_a_legacy_lesson_is_retired_on_any_run_once_and_never_beside_its_re_mined_twin(env):
    db, legacy = legacy_lesson(env)
    write(env, "s1", basic_pair(env["repo"]).text())
    first = mine_now(env)
    assert (first.retired_unrenderable, first.candidates_created) == ({"mined.reason_template": 1}, 1)
    second = mine_now(env, now=START + 2 * DAY)
    assert (second.retired_unrenderable, second.candidates_created) == ({}, 0)
    assert db.execute("select type from events where lesson_id = ? order by seq", (legacy,)).fetchall() == [
        ("proposed",),
        ("retired",),
    ]
    assert db.execute("select avoid, status from lessons where status != 'retired'").fetchall() == [
        ("pytest --frobnicate tests", "candidate")
    ]


def test_a_legacy_lesson_is_retired_even_when_no_transcript_sights_it(env):
    db, legacy = legacy_lesson(env)
    assert mine_now(env).retired_unrenderable == {"mined.reason_template": 1}
    assert store.get_lesson(db, legacy).status is Status.RETIRED


@pytest.mark.parametrize("pinned", [False, True])
def test_the_miner_never_retires_a_promoted_lesson_that_no_longer_renders(env, pinned):
    from scars.mine.build import fingerprint
    from scars.model import EventType

    db, legacy = legacy_lesson(env, fingerprint=fingerprint(RepoScope(RepoId("own-app")), SIG))
    store.apply_event(
        db,
        lesson_id=legacy,
        event=EventType.PROMOTED,
        expected_revision=1,
        actor="human",
        payload={},
        at=START,
    )
    if pinned:
        store.apply_event(
            db,
            lesson_id=legacy,
            event=EventType.PINNED,
            expected_revision=2,
            actor="human",
            payload={},
            at=START,
        )
    db.commit()
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert (summary.retired_unrenderable, summary.candidates_created) == ({}, 0)
    lesson = store.get_lesson(db, legacy)
    assert (lesson.status, lesson.pinned, lesson.text.reason) == (Status.PROMOTED, pinned, FAIL_TEXT)
    assert db.execute("select count(*) from lessons").fetchone() == (1,)


def test_seen_counts_distinct_sessions_not_observations(env):
    write(
        env,
        "s1",
        basic_pair(env["repo"])
        .bash("t4", "pytest --frobnicate tests", FAIL_TEXT)
        .bash("t5", "pytest tests")
        .text(),
    )
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created, summary.observations) == (2, 1, 2)
    db = lessons_db(env)
    assert db.execute("select sessions_seen from lessons").fetchall() == [(1,)]
    write(env, "s2", basic_pair(env["repo"], start=START + 100, prefix="u").text())
    mine_now(env)
    assert db.execute("select sessions_seen from lessons").fetchall() == [(2,)]
    assert store.verify(db) == []


def test_precision_drops_a_broad_pre_trigger_but_keeps_the_error_trigger(env):
    t = Claude(env["repo"])
    for i in range(5):
        t.bash(f"h{i}", "git show bad")
    t.bash("t1", "git log -n bad", "fatal: invalid number of commits").bash("t2", "git log -n 5")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.pre_triggers_dropped) == (1, 1)
    db = lessons_db(env)
    (lesson,) = store.inbox_candidates(db)
    assert lesson.triggers == (Trigger(TriggerKind.ERROR, "fatal: invalid number of commits", Stage.REPAIR),)
    assert db.execute("select matched_failing, matched_passing from precision_runs").fetchall() == [(1, 5)]


def dump(db: sqlite3.Connection) -> list[object]:
    tables = [
        "lessons",
        "triggers",
        "events",
        "observations",
        "miner_offsets",
        "seen_tool_use",
        "pending_failures",
        "precision_runs",
        "mined_calls",
    ]
    return [db.execute(f"select * from {t} order by 1").fetchall() for t in tables]


def test_running_twice_changes_nothing(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    write(env, "s2", Claude(env["repo"]).bash("p1", "pytest --frobnicate x", FAIL_TEXT).text())
    assert mine_now(env) == Summary(
        sessions_read=2, calls=4, failures=2, pairs=1, candidates_created=1, observations=1
    )
    before = dump(lessons_db(env))
    assert [len(rows) for rows in before] == [1, 2, 1, 1, 2, 4, 1, 1, 4]
    assert mine_now(env) == Summary()
    assert dump(lessons_db(env)) == before


# ---------------------------------------------------------------- Codex


def codex_lines(cwd: str) -> list[dict[str, object]]:
    def at(n: int) -> str:
        return iso(START + n)

    def item(n: int, item_id: str, command: list[str], code: int | None, output: str) -> dict[str, object]:
        return {
            "timestamp": at(n),
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": item_id,
                    "command": command,
                    "cwd": cwd,
                    "exit_code": code,
                    "aggregated_output": output,
                },
            },
        }

    return [
        {"timestamp": at(0), "type": "session_meta", "payload": {"id": "sess", "cwd": cwd}},
        {"timestamp": at(1), "type": "turn_context", "payload": {"model": "gpt-test", "cwd": cwd}},
        item(2, "exec-1", ["/bin/zsh", "-lc", "pytest --frobnicate tests"], 2, FAIL_TEXT),
        item(3, "exec-2", ["/bin/zsh", "-lc", "pytest tests"], 0, "1 passed"),
        {
            "timestamp": at(4),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "make lint --strict", "workdir": cwd}),
            },
        },
        {
            "timestamp": at(5),
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Exit code: 2\nWall time: 0.1 seconds\nOutput:\nmake: unknown option strict",
            },
        },
        {
            "timestamp": at(6),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c2",
                "arguments": json.dumps({"cmd": "make lint"}),
            },
        },
        {
            "timestamp": at(7),
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c2",
                "output": "make: no exit code shown",
            },
        },
    ]


def test_codex_rollout_pairs_on_visible_exit_codes_only(env, tmp_path):
    sessions = tmp_path / "codex" / "2027" / "01" / "15"
    sessions.mkdir(parents=True)
    name = "rollout-2027-01-15T00-00-00-0000000a-0000-4000-8000-000000000001.jsonl"
    (sessions / name).write_text("".join(json.dumps(r) + "\n" for r in codex_lines(env["repo"])))
    source = Source(Harness.CODEX, tmp_path / "codex", "**/rollout-*.jsonl")
    summary = mine_now(env, sources=[source])
    assert (summary.calls, summary.failures, summary.pairs, summary.candidates_created) == (4, 2, 1, 1)
    db = lessons_db(env)
    assert db.execute("select harness, session_id, failed_tool_use_id from observations").fetchall() == [
        ("codex", "0000000a-0000-4000-8000-000000000001", "exec-1")
    ]
    assert db.execute("select status, model_id from mined_calls where tool_use_id = 'c2'").fetchall() == [
        ("unknown", "gpt-test")
    ]
    assert db.execute("select tool_use_id from pending_failures").fetchall() == [("c1",)]


def test_codex_static_fixture(env, tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    for f in (FIXTURES / "codex").glob("rollout-*.jsonl"):
        copy_fixture(f, root / f.name, env["repo"])
    summary = mine_now(env, sources=[Source(Harness.CODEX, root, "**/rollout-*.jsonl")])
    assert (summary.pairs, summary.candidates_created) == (1, 1)


def test_codex_reader_trusts_only_visible_exit_codes():
    from scars.mine import codex
    from scars.mine.calls import CallStatus, Line

    def item(n, code):
        return {
            "timestamp": iso(START + n),
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": f"e{n}",
                    "command": "ls",
                    "cwd": "/r",
                    "exit_code": code,
                    "aggregated_output": "ls: x: No such file",
                },
            },
        }

    def output(n, text):
        return [
            {
                "timestamp": iso(START + n),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": f"f{n}",
                    "arguments": json.dumps({"command": ["bash", "-lc", "ls"]}),
                },
            },
            {
                "timestamp": iso(START + n),
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": f"f{n}", "output": text},
            },
        ]

    records = [
        item(1, None),
        item(2, 0),
        item(3, 1),
        *output(4, "no header here"),
        *output(5, json.dumps({"output": "ls: x: No such file", "metadata": {"exit_code": 1}})),
        *output(6, "Process exited with code 0\nOutput:\nok"),
    ]
    parsed = codex.parse([Line(i, r) for i, r in enumerate(records)], "s")
    assert [(c.tool_use_id, c.status, c.output) for c in parsed.calls] == [
        ("e1", CallStatus.UNKNOWN, ""),
        ("e2", CallStatus.OK, ""),
        ("e3", CallStatus.FAILED, "ls: x: No such file"),
        ("f4", CallStatus.UNKNOWN, ""),
        ("f5", CallStatus.FAILED, "ls: x: No such file"),
        ("f6", CallStatus.OK, ""),
    ]
    assert {c.input for c in parsed.calls} == {"ls"}
    assert parsed.resume_offset is None


# ---------------------------------------------------------------- outcome pass wiring


def test_outcome_pass_writes_outcomes_and_backfills_models(env):
    t = basic_pair(env["repo"])
    write(env, "s1", t.text())
    tdb = store.init_telemetry(env["home"] / "telemetry.db")
    tdb.execute(
        "insert into hits (seq, at, harness, session_id, tool_use_id, event, stage, lesson_id, outcome) "
        "values (1, ?, 'claude', 's1', 't1', 'tool_failed', 'repair', 'L-x', 'shown')",
        (START + 11,),
    )
    tdb.execute(
        "insert into hits (seq, at, harness, session_id, event, outcome) "
        "values (2, ?, 'claude', 's1', 'prompt', 'no_match')",
        (START + 5,),
    )
    summary = mine_now(env, now=START + 2 * DAY)
    assert (summary.outcomes_written, summary.models_backfilled) == (1, 2)
    assert tdb.execute("select hit_seq, outcome from outcomes").fetchall() == [(1, "heeded")]
    assert tdb.execute("select model_id from hits order by seq").fetchall() == [
        ("claude-test-1",),
        ("claude-test-1",),
    ]
    assert mine_now(env, now=START + 2 * DAY) == Summary()


def test_missing_telemetry_is_skipped(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.outcomes_written) == (1, 0)
    assert not (env["home"] / "telemetry.db").exists()


# ---------------------------------------------------------------- review fixes

TOKEN = "ghp_" + "a" * 36
GATE = Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.GATE)


def the_lesson(env):
    (lesson,) = store.inbox_candidates(lessons_db(env)) + store.active_for_hook(
        lessons_db(env), RepoId("own-app")
    )
    return lesson


def test_mined_pre_trigger_is_stored_at_the_gate_stage(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    mine_now(env)
    assert set(the_lesson(env).triggers) == {Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR), GATE}


def failure_event(cwd: str, session: str = "later"):
    from scars.model import HookEvent, HookEventKind, ToolKind

    return HookEvent(
        Harness.CLAUDE,
        "1",
        HookEventKind.TOOL_FAILED,
        session,
        "tu9",
        cwd,
        ToolKind.SHELL,
        "pytest --frobnicate tests",
        (),
        FAIL_TEXT,
        None,
        None,
    )


def test_mined_lesson_is_selected_at_repair_and_shown_by_the_hook(env, monkeypatch):
    from scars import hook, match, telemetry
    from scars.model import EventType, HitOutcome, HookEvent, HookEventKind

    write(env, "s1", basic_pair(env["repo"]).text())
    mine_now(env)
    monkeypatch.setenv("SCARS_HOME", str(env["home"]))
    monkeypatch.setattr(hook, "git_common_dir", lambda cwd: "/git/app")
    monkeypatch.setattr(hook, "repo_root", lambda cwd: env["repo"])
    own_session = failure_event(env["repo"], session="s1")
    unreviewed = hook.decide(own_session, "PostToolUseFailure", telemetry.EMPTY, now=START + 2 * DAY)
    assert (unreviewed.outcome, unreviewed.shown) == (HitOutcome.NO_MATCH, [])
    db = lessons_db(env)
    (candidate,) = store.inbox_candidates(db)
    store.apply_event(
        db,
        lesson_id=candidate.id,
        event=EventType.PROMOTED,
        expected_revision=candidate.revision,
        actor="human",
        payload={},
        at=START + DAY,
    )
    lesson = the_lesson(env)
    event = failure_event(env["repo"])
    context = match.MatchContext(env["repo"], RepoId("own-app"), SIG)
    assert match.select([lesson], event, context).selected == (
        match.Selected(lesson, Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR), f"error:repair:{SIG}"),
    )
    pre_tool = HookEvent(**{**event.__dict__, "event": HookEventKind.PRE_TOOL, "error_text": None})
    assert match.matches(GATE, pre_tool) is True
    assert match.select([lesson], pre_tool, context).selected == ()
    decision = hook.decide(event, "PostToolUseFailure", telemetry.EMPTY, now=START + 2 * DAY)
    assert decision.outcome is HitOutcome.SHOWN
    assert [s.lesson_id for s in decision.shown] == [lesson.id]
    text = json.dumps(decision.output)
    assert "AVOID: pytest --frobnicate" in text and "INSTEAD: run `pytest tests` instead" in text


def test_secret_in_a_failed_command_is_not_kept_as_a_pending_failure(env):
    write(env, "s1", Claude(env["repo"]).bash("t1", f"pytest --bad --token={TOKEN}", FAIL_TEXT).text())
    summary = mine_now(env)
    assert (summary.calls, summary.failures) == (1, 1)
    db = lessons_db(env)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0
    assert db.execute("select tool_use_id, command from mined_calls").fetchall() == [("t1", None)]


def test_secret_in_an_error_or_a_path_is_not_stored_in_mined_calls(env):
    t = (
        Claude(env["repo"])
        .bash("t1", "git push origin main", f"fatal: {TOKEN}")
        .call("t2", "Read", {"file_path": f"/private/{TOKEN}/notes.md"})
        .call("t3", "Read", {"file_path": f"{env['repo']}/README.md"})
    )
    write(env, "s1", t.text())
    mine_now(env)
    db = lessons_db(env)
    rows = db.execute("select tool_use_id, error_signature, paths from mined_calls order by seq").fetchall()
    assert rows == [
        ("t1", None, "[]"),
        ("t2", None, "[]"),
        ("t3", None, json.dumps([f"{env['repo']}/README.md"])),
    ]
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0
    stored = "\n".join(str(r) for t in dump(db) for r in t)
    assert TOKEN not in stored


def test_secret_in_the_error_signature_writes_no_observation(env):
    t = (
        Claude(env["repo"])
        .bash("t1", "git push origin main", f"fatal: {TOKEN}")
        .bash("t2", "git push origin")
    )
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.calls, summary.pairs, summary.candidates_created, summary.observations) == (2, 1, 0, 0)
    assert summary.rejected == {"secret.github_token": 1}
    assert TOKEN not in "\n".join(str(r) for t in dump(lessons_db(env)) for r in t)


def boom_once(monkeypatch, name, when=lambda kwargs: True):
    real = getattr(store, name)
    fired = []

    def patched(*args, **kwargs):
        if not fired and when(kwargs):
            fired.append(1)
            raise RuntimeError(f"crash in {name}")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, name, patched)
    return fired


@pytest.mark.parametrize(
    ("name", "when"),
    [
        ("create_lesson", lambda kw: True),
        ("record_precision", lambda kw: True),
        ("add_observation", lambda kw: True),
    ],
)
def test_a_crash_while_writing_a_new_lesson_leaves_nothing_behind(env, monkeypatch, name, when):
    write(env, "s1", basic_pair(env["repo"]).text())
    fired = boom_once(monkeypatch, name, when)
    with pytest.raises(RuntimeError):
        mine_now(env)
    assert fired == [1]
    summary = mine_now(env)
    assert (summary.candidates_created, summary.observations) == (1, 1)
    db = lessons_db(env)
    assert db.execute("select status, sessions_seen from lessons").fetchall() == [("candidate", 1)]
    assert db.execute("select type from events order by seq").fetchall() == [
        ("proposed",),
    ]
    assert store.verify(db) == []


def test_a_crash_between_observed_and_the_observation_does_not_double_count(env, monkeypatch):
    write(env, "s1", basic_pair(env["repo"]).text())
    mine_now(env)
    write(env, "s2", basic_pair(env["repo"], start=START + 100, prefix="u").text())
    boom_once(monkeypatch, "add_observation")
    with pytest.raises(RuntimeError):
        mine_now(env)
    summary = mine_now(env)
    assert (summary.pairs, summary.observations) == (1, 1)
    db = lessons_db(env)
    assert db.execute("select sessions_seen from lessons").fetchall() == [(2,)]
    assert db.execute("select type from events order by seq").fetchall() == [
        ("proposed",),
        ("observed",),
    ]


def codex_record(n: int, kind: str, payload: dict[str, object]) -> str:
    return json.dumps({"timestamp": iso(START + n), "type": kind, "payload": payload}) + "\n"


def command_item(n: int, item_id: str, command: str, code: int, output: str = "") -> str:
    item = {"type": "CommandExecution", "id": item_id, "command": command, "exit_code": code}
    return codex_record(
        n, "event_msg", {"type": "item_completed", "item": {**item, "aggregated_output": output}}
    )


def test_codex_context_carries_over_to_the_next_batch(env, tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    path = root / "rollout-2027-01-15T00-00-00-0000000a-0000-4000-8000-000000000003.jsonl"
    path.write_text(
        codex_record(0, "session_meta", {"id": "s", "cwd": env["repo"]})
        + codex_record(1, "turn_context", {"model": "gpt-test", "cwd": env["repo"]})
        + command_item(2, "e1", "pytest --frobnicate tests", 2, FAIL_TEXT)
    )
    source = Source(Harness.CODEX, root, "**/rollout-*.jsonl")
    assert mine_now(env, sources=[source]).failures == 1
    with path.open("a") as f:
        f.write(command_item(3, "e2", "pytest tests", 0))
    summary = mine_now(env, sources=[source])
    assert (summary.pairs, summary.candidates_created) == (1, 1)
    db = lessons_db(env)
    assert db.execute("select cwd, model_id from mined_calls where tool_use_id = 'e2'").fetchall() == [
        (env["repo"], "gpt-test")
    ]


def use(t: Claude, tool_id: str, name: str, args: dict[str, object]) -> None:
    t.at += 10
    t.records.append(
        {
            "type": "assistant",
            "timestamp": iso(t.at),
            "cwd": t.cwd,
            "message": {
                "model": "m",
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
            },
        }
    )


def result(t: Claude, tool_id: str) -> None:
    t.at += 10
    t.records.append(
        {
            "type": "user",
            "timestamp": iso(t.at),
            "cwd": t.cwd,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "is_error": False, "content": "ok"}
                ]
            },
        }
    )


EDIT_ARGS = {"file_path": "a.py", "old_string": "a", "new_string": "b"}


def test_an_edit_whose_result_arrives_after_the_fix_still_breaks_the_pair(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    use(t, "e1", "Edit", EDIT_ARGS)
    use(t, "t3", "Bash", {"command": "pytest tests"})
    result(t, "t3")
    result(t, "e1")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.calls, summary.failures, summary.pairs, summary.candidates_created) == (3, 1, 0, 0)


def test_an_edit_with_no_result_yet_breaks_the_pair(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    use(t, "e1", "Edit", EDIT_ARGS)
    use(t, "t3", "Bash", {"command": "pytest tests"})
    result(t, "t3")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.failures, summary.pairs) == (1, 0)
    assert lessons_db(env).execute("select count(*) from pending_failures").fetchone()[0] == 0


def test_an_unresolved_earlier_call_counts_toward_the_window(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    for i in range(8):
        t.read(f"r{i}")
    use(t, "slow", "Read", {"file_path": "b.py"})
    use(t, "t3", "Bash", {"command": "pytest tests"})
    result(t, "t3")
    path = write(env, "s1", t.text())
    first = mine_now(env)
    assert (first.calls, first.pairs) == (9, 0)
    t.records = []
    result(t, "slow")
    with path.open("a") as f:
        f.write(t.text())
    second = mine_now(env)
    assert (second.calls, second.pairs, second.candidates_created) == (2, 1, 1)


def test_an_unresolved_call_pushes_the_fix_out_of_the_window(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    for i in range(9):
        t.read(f"r{i}")
    use(t, "slow", "Read", {"file_path": "b.py"})
    use(t, "t3", "Bash", {"command": "pytest tests"})
    result(t, "t3")
    path = write(env, "s1", t.text())
    assert mine_now(env).pairs == 0
    t.records = []
    result(t, "slow")
    with path.open("a") as f:
        f.write(t.text())
    second = mine_now(env)
    assert (second.calls, second.pairs) == (2, 0)
    assert lessons_db(env).execute("select count(*) from pending_failures").fetchone()[0] == 0


def telemetry_with(env, *hits):
    tdb = store.init_telemetry(env["home"] / "telemetry.db")
    for seq, at, session, stage, lesson, outcome, key in hits:
        tdb.execute(
            "insert into hits (seq, at, harness, session_id, event, stage, lesson_id, outcome, trigger_key) "
            "values (?, ?, 'claude', ?, 'prompt', ?, ?, ?, ?)",
            (seq, at, session, stage, lesson, outcome, key),
        )
    return tdb


def test_a_session_with_a_recent_hit_has_not_ended(env):
    write(env, "s1", Claude(env["repo"]).read("t1").text())
    tdb = telemetry_with(
        env,
        (1, START + 20, "s1", "plan", "L-x", "shown", "prompt:plan:deploy"),
        (2, START + 20 * 3600, "s1", None, None, "no_match", None),
    )
    assert mine_now(env, now=START + 25 * 3600).outcomes_written == 0
    assert tdb.execute("select count(*) from outcomes").fetchone()[0] == 0
    assert mine_now(env, now=START + 44 * 3600 + 1).outcomes_written == 1
    assert tdb.execute("select hit_seq, outcome from outcomes").fetchall() == [(1, "no_attempt")]


def add_path_lesson(env, *triggers: Trigger) -> str:
    db = lessons_db(env)
    from scars.model import LessonText, RepoScope

    return store.create_lesson(
        db,
        text=LessonText("pytest --frobnicate", "run `pytest tests` instead", SIG),
        scope=RepoScope(RepoId("own-app")),
        trust_class=TrustClass.OWN_CODE,
        triggers=(Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR), *triggers),
        fingerprint="fp-path",
        actor="test",
        at=START,
    )


def mine_with_roots(env, now):
    sources = [Source(Harness.CLAUDE, env["projects"], "*/*.jsonl")]
    return mine.run(
        env["home"], sources, now, common_dir_of=common_dir_of, repo_root_of=lambda cwd: env["repo"]
    )


def test_plan_hit_on_a_path_trigger_finds_absolute_paths_under_the_repo_root(env):
    lesson = add_path_lesson(env, Trigger(TriggerKind.PATH, "src/**", Stage.PLAN))
    t = Claude(env["repo"]).call("t1", "Edit", {**EDIT_ARGS, "file_path": f"{env['repo']}/src/app.py"})
    write(env, "s1", t.text())
    tdb = telemetry_with(env, (1, START, "s1", "plan", lesson, "shown", "path:plan:src/**"))
    assert mine_with_roots(env, START + 2 * DAY).outcomes_written == 1
    assert tdb.execute("select outcome from outcomes").fetchall() == [("fired_unneeded",)]


def test_plan_hit_is_attributed_to_the_trigger_that_fired(env):
    lesson = add_path_lesson(env, Trigger(TriggerKind.PATH, "src/**", Stage.PLAN), GATE)
    write(env, "s1", Claude(env["repo"]).bash("t1", "pytest --frobnicate tests").text())
    tdb = telemetry_with(env, (1, START, "s1", "plan", lesson, "shown", "path:plan:src/**"))
    mine_with_roots(env, START + 2 * DAY)
    assert tdb.execute("select outcome from outcomes").fetchall() == [("no_attempt",)]


def test_observation_passing_input_is_the_command_in_instead(env):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT).bash("t2", "pytest   tests")
    write(env, "s1", t.text())
    mine_now(env)
    lesson = the_lesson(env)
    assert lesson.text.instead == "run `pytest tests` instead"
    assert store.passing_inputs(lessons_db(env), [lesson.id]) == {lesson.id: frozenset({"pytest tests"})}


def test_a_passing_command_over_the_cap_drops_the_candidate(env):
    long = "pytest " + " ".join(f"tests/test_{i:04d}.py" for i in range(80))
    t = Claude(env["repo"]).bash("t1", long + " --frobnicate", FAIL_TEXT).bash("t2", long)
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created) == (1, 0)
    assert summary.skipped == {"passing_input_too_long": 1}


def test_no_distinguishing_argument_means_no_pre_trigger_and_the_full_avoid(env):
    t = (
        Claude(env["repo"])
        .bash("t1", "pytest tests", FAIL_TEXT)
        .bash("t2", "pytest tests -p no:cacheprovider")
    )
    write(env, "s1", t.text())
    mine_now(env)
    lesson = the_lesson(env)
    assert lesson.triggers == (Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR),)
    assert lesson.text.avoid == "pytest tests"


def test_a_candidate_over_the_repair_budget_is_dropped(env):
    fix = "pytest " + " ".join(f"tests/unit/test_mod{i}.py" for i in range(9))
    t = Claude(env["repo"]).bash("t1", fix + " --frobnicate", FAIL_TEXT).bash("t2", fix)
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created) == (1, 0)
    assert summary.skipped == {"over_repair_budget": 1}


def test_a_cwd_that_no_longer_exists_is_unknown_and_never_pairs(env):
    write(env, "s1", basic_pair(env["repo"] + "/gone/deeper").text())
    summary = mine_now(env)
    assert (summary.failures, summary.pairs, summary.candidates_created) == (1, 0, 0)


def test_an_empty_cwd_is_unknown_and_never_pairs(env):
    write(env, "s1", basic_pair("").text())
    summary = mine_now(env)
    assert (summary.failures, summary.pairs, summary.candidates_created) == (1, 0, 0)


def test_unregistered_directories_compare_by_realpath(env):
    link = Path(env["loose"]).parent / "loose-link"
    link.symlink_to(env["loose"])
    t = Claude(env["loose"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    t.cwd = str(link)
    t.bash("t2", "pytest tests")
    write(env, "s1", t.text())
    assert mine_now(env).pairs == 1


def test_a_claude_call_runs_in_its_invocation_cwd_not_the_result_records(env):
    t = Claude(env["upstream"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT).bash("t2", "pytest tests")
    for record in t.records:
        if record["type"] == "user":
            record["cwd"] = env["repo"]
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert summary.pairs == 1
    assert lessons_db(env).execute("select cwd from mined_calls order by tool_use_id").fetchall() == [
        (env["upstream"],),
        (env["upstream"],),
    ]
    assert the_lesson(env).trust_class is TrustClass.THIRD_PARTY_REPO


def test_a_codex_file_url_cwd_is_decoded_to_a_path(env, tmp_path):
    from urllib.parse import quote

    spaced = Path(env["loose"]) / "my app"
    spaced.mkdir()
    url = "file://" + quote(str(spaced))
    root = tmp_path / "codex"
    root.mkdir()
    (root / "rollout-2027-01-15T00-00-00-0000000a-0000-4000-8000-000000000009.jsonl").write_text(
        codex_record(0, "session_meta", {"id": "s", "cwd": url})
        + codex_record(1, "turn_context", {"model": "gpt-test", "cwd": url})
        + command_item(2, "e1", "pytest --frobnicate tests", 2, FAIL_TEXT)
        + command_item(3, "e2", "pytest tests", 0)
    )
    summary = mine_now(env, sources=[Source(Harness.CODEX, root, "**/rollout-*.jsonl")])
    assert (summary.pairs, summary.candidates_created) == (1, 1)
    assert lessons_db(env).execute("select cwd from mined_calls order by tool_use_id").fetchall() == [
        (str(spaced),),
        (str(spaced),),
    ]


@pytest.mark.parametrize(("host", "pairs"), [("localhost", 1), ("build-box", 0)])
def test_a_codex_file_url_on_another_host_is_an_unknown_directory(env, tmp_path, host, pairs):
    url = f"file://{host}{env['loose']}"
    root = tmp_path / "codex"
    root.mkdir()
    (root / "rollout-2027-01-15T00-00-00-0000000a-0000-4000-8000-00000000000a.jsonl").write_text(
        codex_record(0, "session_meta", {"id": "s", "cwd": url})
        + codex_record(1, "turn_context", {"model": "gpt-test", "cwd": url})
        + command_item(2, "e1", "pytest --frobnicate tests", 2, FAIL_TEXT)
        + command_item(3, "e2", "pytest tests", 0)
    )
    summary = mine_now(env, sources=[Source(Harness.CODEX, root, "**/rollout-*.jsonl")])
    assert (summary.pairs, summary.candidates_created) == (pairs, pairs)


def test_cd_into_a_literal_dir_then_one_command_is_that_command_run_there(env):
    work = str(Path(env["repo"]).parent)
    t = (
        Claude(work)
        .bash("t1", "cd app && pytest  --frobnicate tests", FAIL_TEXT)
        .bash("t2", f"cd {env['repo']} && pytest tests")
    )
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created, summary.skipped) == (1, 1, {})
    lesson = the_lesson(env)
    assert (lesson.scope, lesson.text.avoid, lesson.text.instead) == (
        RepoScope(RepoId("own-app")),
        "pytest --frobnicate tests",
        "run `pytest tests` instead",
    )
    assert lessons_db(env).execute(
        "select command, cwd from mined_calls order by tool_use_id"
    ).fetchall() == [
        ("pytest --frobnicate tests", env["repo"]),
        ("pytest tests", env["repo"]),
    ]


@pytest.mark.parametrize(
    "failed",
    [
        "cd app && pytest --frobnicate tests && echo done",
        "cd app; pytest --frobnicate tests",
        "cd app && cd . && pytest --frobnicate tests",
        "cd app && pytest --frobnicate tests > log",
        "cd $APP && pytest --frobnicate tests",
    ],
)
def test_any_other_compound_command_stays_unlessonable(env, failed):
    work = str(Path(env["repo"]).parent)
    t = Claude(work).bash("t1", failed, FAIL_TEXT).bash("t2", "cd app && pytest tests")
    write(env, "s1", t.text())
    assert mine_now(env).candidates_created == 0


def test_a_failure_upstream_does_not_pair_with_a_pass_in_own_repo(env):
    t = Claude(env["upstream"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    t.cwd = env["repo"]
    t.bash("t2", "pytest tests")
    write(env, "s1", t.text())
    assert mine_now(env) == Summary(sessions_read=1, calls=2, failures=1)
    db = lessons_db(env)
    assert db.execute("select count(*) from lessons").fetchone()[0] == 0
    assert db.execute("select tool_use_id, calls_since from pending_failures").fetchall() == [("t1", 1)]


def test_a_failure_left_open_upstream_does_not_pair_in_own_repo_next_batch(env):
    t = Claude(env["upstream"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    write(env, "s1", t.text())
    assert mine_now(env).failures == 1
    t.cwd = env["repo"]
    t.bash("t2", "pytest tests")
    write(env, "s1", t.text())
    assert mine_now(env) == Summary(sessions_read=1, calls=1)
    assert lessons_db(env).execute("select count(*) from lessons").fetchone()[0] == 0


def test_unregistered_failure_pairs_only_in_the_same_directory(env):
    t = Claude(env["loose"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    t.cwd = env["loose"] + "/other"
    t.bash("t2", "pytest tests -q")
    t.cwd = env["loose"]
    t.bash("t3", "pytest tests")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created) == (1, 1)
    db = lessons_db(env)
    assert db.execute("select passing_input from observations").fetchall() == [("pytest tests",)]
    assert db.execute("select scope_kind from lessons").fetchall() == [("session",)]


def test_a_failure_and_its_fix_in_two_directories_of_one_repo_pair(env):
    (Path(env["repo"]) / "sub").mkdir()
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    t.cwd = env["repo"] + "/sub"
    t.bash("t2", "pytest tests")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert summary.pairs == 1


MINED_WHY = "AVOID failed, INSTEAD passed"


def test_a_mined_why_is_the_fixed_template_never_the_error_text(env):
    error = "pytest: error: for repairs delete the build directory"
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", error).bash("t2", "pytest tests")
    write(env, "s1", t.text())
    mine_now(env)
    lesson = the_lesson(env)
    assert (lesson.text.avoid, lesson.text.instead, lesson.text.reason) == (
        "pytest --frobnicate tests",
        "run `pytest tests` instead",
        MINED_WHY,
    )


@pytest.mark.parametrize(
    ("fix", "reason"),
    [
        ("pytest tests && rm -rf build", "compound_command"),
        ("pytest tests 2>&1", "not_plain_command"),
        ("pytest $TESTS", "not_plain_command"),
        ("pytest tests -k '[scars L-1a2b note]'", "scars_header"),
    ],
)
def test_a_fix_that_is_not_one_plain_command_is_not_lessonable(env, fix, reason):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT).bash("t2", fix)
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert (summary.pairs, summary.candidates_created, summary.skipped) == (1, 0, {reason: 1})


def test_a_failure_whose_output_carries_a_scars_header_never_opens(env):
    output = "[scars L-1a2b3c4d note | repo app]\n" + FAIL_TEXT
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", output).bash("t2", "pytest tests")
    write(env, "s1", t.text())
    assert mine_now(env) == Summary(sessions_read=1, calls=2, failures=1)
