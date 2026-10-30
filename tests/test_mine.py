import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scars import mine, store
from scars.mine import Source, Summary
from scars.model import Harness, RepoId, Stage, Status, Trigger, TriggerKind, TrustClass

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
T0 = 1_800_000_000.0
DAY = 86400.0
FAIL_TEXT = "pytest: error: unrecognized arguments: --frobnicate"
SIG = "pytest: error: unrecognized arguments: --frobnicate"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


class Claude:
    """Builds a synthetic Claude Code transcript, one tool call at a time."""

    def __init__(self, cwd: str, start: float = T0) -> None:
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
def env(tmp_path: Path) -> dict[str, object]:
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
    if cwd.endswith("/app"):
        return "/git/app"
    if cwd.endswith("/lib"):
        return "/git/lib"
    return None


def mine_now(env: dict[str, object], now: float = T0 + DAY, sources: list[Source] | None = None) -> Summary:
    sources = sources or [Source(Harness.CLAUDE, env["projects"], "*/*.jsonl")]  # type: ignore[arg-type]
    return mine.run(env["home"], sources, now, common_dir_of=common_dir_of)  # type: ignore[arg-type]


def write(env: dict[str, object], name: str, text: str) -> Path:
    path = env["projects"] / "p" / f"{name}.jsonl"  # type: ignore[operator]
    path.write_text(text)
    return path


def lessons_db(env: dict[str, object]) -> sqlite3.Connection:
    return store.connect(env["home"] / "lessons.db")  # type: ignore[operator]


def basic_pair(cwd: str, start: float = T0, prefix: str = "t") -> Claude:
    return (
        Claude(cwd, start)
        .bash(f"{prefix}1", "pytest --frobnicate tests", FAIL_TEXT)
        .read(f"{prefix}2")
        .bash(f"{prefix}3", "pytest tests")
    )


def test_pair_in_own_repo_becomes_provisional_with_both_triggers(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert summary == Summary(
        sessions_read=1, calls=3, failures=1, pairs=1, candidates_created=1, observations=1, provisionalized=1
    )
    db = lessons_db(env)
    (lesson,) = store.inbox_candidates(db) + store.active_for_hook(db, RepoId("own-app"), "x")
    assert lesson.status is Status.PROVISIONAL
    assert lesson.trust_class is TrustClass.OWN_CODE
    assert (lesson.text.avoid, lesson.text.instead, lesson.text.reason) == (
        "pytest --frobnicate",
        "run `pytest tests` instead",
        SIG,
    )
    assert set(lesson.triggers) == {
        Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR),
        Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.PLAN),
    }
    assert db.execute("select failed_input, passing_input, session_id from observations").fetchall() == [
        ("pytest --frobnicate tests", "pytest tests", "s1")
    ]
    assert db.execute(
        "select revision, matched_failing, matched_passing, window_days from precision_runs"
    ).fetchall() == [(2, 1, 0, 30)]
    assert db.execute("select expires_at from lessons").fetchone()[0] == T0 + 31 + 14 * DAY
    assert store.verify(db) == []


def test_static_fixture_pairs_like_the_builder(env):
    (env["projects"] / "p" / "fixture.jsonl").write_bytes((FIXTURES / "claude" / "pair.jsonl").read_bytes())
    assert mine_now(env).candidates_created == 1


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
    assert mine_now(env).pairs == 0


@pytest.mark.parametrize(("between", "pairs"), [(9, 1), (10, 0)])
def test_fix_must_come_within_ten_calls(env, between, pairs):
    t = Claude(env["repo"]).bash("t1", "pytest --frobnicate tests", FAIL_TEXT)
    for i in range(between):
        t.read(f"r{i}")
    t.bash("fix", "pytest tests")
    write(env, "s1", t.text())
    summary = mine_now(env)
    assert summary.pairs == pairs
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
    mine_now(env, now=T0 + 3600)
    db = lessons_db(env)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 1
    mine_now(env, now=T0 + 11 + DAY - 1)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 1
    mine_now(env, now=T0 + 11 + DAY)
    assert db.execute("select count(*) from pending_failures").fetchone()[0] == 0


def test_upstream_repo_pair_stays_a_third_party_candidate(env):
    write(env, "s1", basic_pair(env["upstream"]).text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.provisionalized) == (1, 0)
    (lesson,) = store.inbox_candidates(lessons_db(env))
    assert (lesson.status, lesson.trust_class, lesson.scope.kind) == (
        Status.CANDIDATE,
        TrustClass.THIRD_PARTY_REPO,
        "repo",
    )


def test_pair_outside_registered_repos_is_a_session_candidate(env):
    write(env, "s1", basic_pair(env["loose"]).text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.provisionalized) == (1, 0)
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


def test_secret_outside_the_lesson_text_still_drops_the_candidate(env):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    t = (
        Claude(env["repo"])
        .bash("t1", f"pytest --frobnicate tests; echo {token}", FAIL_TEXT)
        .bash("t2", "pytest tests")
    )
    write(env, "s1", t.text())
    assert mine_now(env).rejected == {"secret.github_token": 1}


def test_lessons_expire_fourteen_days_after_the_last_sighting(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    last_seen = T0 + 31
    assert mine_now(env, now=last_seen + 14 * DAY - 1).expired == 0
    summary = mine_now(env, now=last_seen + 14 * DAY)
    assert summary.expired == 1
    db = lessons_db(env)
    assert db.execute("select status from lessons").fetchall() == [("retired",)]
    assert store.verify(db) == []


def test_second_sighting_observes_the_same_lesson_and_resets_expiry(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    write(env, "s2", basic_pair(env["repo"], start=T0 + 5 * DAY, prefix="u").text())
    summary = mine_now(env, now=T0 + 6 * DAY)
    assert (summary.pairs, summary.candidates_created, summary.observations, summary.provisionalized) == (
        2,
        1,
        2,
        1,
    )
    db = lessons_db(env)
    assert db.execute("select status, sessions_seen, last_seen_at, fix_programs from lessons").fetchall() == [
        ("provisional", 2, T0 + 5 * DAY + 31, '["pytest"]')
    ]
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
    (lesson,) = store.active_for_hook(db, RepoId("own-app"), "x")
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
    mine_now(env)
    before = dump(lessons_db(env))
    assert mine_now(env) == Summary()
    assert dump(lessons_db(env)) == before


# ---------------------------------------------------------------- Codex


def codex_lines(cwd: str) -> list[dict[str, object]]:
    def at(n: int) -> str:
        return iso(T0 + n)

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
        (root / f.name).write_bytes(f.read_bytes())
    summary = mine_now(env, sources=[Source(Harness.CODEX, root, "**/rollout-*.jsonl")])
    assert (summary.pairs, summary.candidates_created) == (1, 1)


def test_codex_reader_trusts_only_visible_exit_codes():
    from scars.mine import codex
    from scars.mine.calls import CallStatus, Line

    def item(n, code):
        return {
            "timestamp": iso(T0 + n),
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
                "timestamp": iso(T0 + n),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": f"f{n}",
                    "arguments": json.dumps({"command": ["bash", "-lc", "ls"]}),
                },
            },
            {
                "timestamp": iso(T0 + n),
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
        (T0 + 11,),
    )
    tdb.execute(
        "insert into hits (seq, at, harness, session_id, event, outcome) "
        "values (2, ?, 'claude', 's1', 'prompt', 'no_match')",
        (T0 + 5,),
    )
    summary = mine_now(env, now=T0 + 2 * DAY)
    assert (summary.outcomes_written, summary.models_backfilled) == (1, 2)
    assert tdb.execute("select hit_seq, outcome from outcomes").fetchall() == [(1, "heeded")]
    assert tdb.execute("select model_id from hits order by seq").fetchall() == [
        ("claude-test-1",),
        ("claude-test-1",),
    ]
    assert mine_now(env, now=T0 + 2 * DAY) == Summary()


def test_missing_telemetry_is_skipped(env):
    write(env, "s1", basic_pair(env["repo"]).text())
    summary = mine_now(env)
    assert (summary.candidates_created, summary.outcomes_written) == (1, 0)
    assert not (env["home"] / "telemetry.db").exists()
