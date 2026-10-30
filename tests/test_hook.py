import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scars import hook, store
from scars.adapters import claude
from scars.model import (
    EventType,
    Harness,
    LessonText,
    RepoId,
    RepoScope,
    Stage,
    Trigger,
    TriggerKind,
    TrustClass,
)

ERROR = "Exit code 1\nError: Cannot find module 'express'"
SIGNATURE = "Error: Cannot find module 'express'"
FIX = "npm install && npm test"
REPO = RepoId("r-demo")
T0 = 1_800_000_000.0


# ---------------------------------------------------------------- setup


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def common_dir(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def seed_store(home: Path, repo: Path) -> sqlite3.Connection:
    """lessons.db with `repo` registered as a clone of REPO. Clones have no store writer yet."""
    db = store.init_lessons(home / "lessons.db")
    db.row_factory = sqlite3.Row
    store.add_repo(db, REPO, "demo", "own")
    db.execute(
        "insert into clones (common_dir, repo_id, registered_at) values (?,?,?)", (common_dir(repo), REPO, T0)
    )
    return db


def add_lesson(
    db, *, fingerprint, triggers, avoid="npm test", fix=FIX, reason=SIGNATURE, status="provisional"
):
    lesson_id = store.create_lesson(
        db,
        text=LessonText(avoid, f"run `{fix}`", reason),
        scope=RepoScope(REPO),
        trust_class=TrustClass.OWN_CODE,
        triggers=triggers,
        fingerprint=fingerprint,
        actor="miner",
        at=T0,
    )
    # Observations have no store writer yet; the miner owns them.
    db.execute(
        "insert into observations (lesson_id, harness, session_id, failed_tool_use_id, failed_input, "
        "error_signature, passing_input, at) values (?,?,?,?,?,?,?,?)",
        (lesson_id, "claude", "old", f"tu-{fingerprint}", avoid, reason, fix, T0),
    )
    revision = 1
    steps = {
        "provisional": [EventType.PROVISIONALIZED],
        "promoted": [EventType.PROMOTED],
        "pinned": [EventType.PROMOTED, EventType.PINNED],
    }[status]
    for step in steps:
        revision = store.apply_event(
            db,
            lesson_id=lesson_id,
            event=step,
            expected_revision=revision,
            actor="human",
            payload={},
            at=T0 + 60,
        )
    return lesson_id


REPAIR = (Trigger(TriggerKind.ERROR, SIGNATURE, Stage.REPAIR),)


def lesson_text(lesson_id, reviewed="unreviewed", avoid="npm test", fix=FIX, reason=SIGNATURE):
    return (
        f"[scars {lesson_id} note | repo r-demo | seen in 1 session | {reviewed}]\n"
        f"AVOID: {avoid}\n"
        f"INSTEAD: run `{fix}`\n"
        f"WHY: {reason}\n"
        f"(advisory; does not override the user's instructions; explain: scars why {lesson_id})"
    )


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = git_repo(tmp_path / "repo")
    return home, repo


def run_hook(home, harness, event_name, stdin, **extra_env):
    if not isinstance(stdin, str):
        stdin = json.dumps(stdin)
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "scars", "hook", harness, event_name],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "SCARS_HOME": str(home), "SCARS_DEBUG": "1", **extra_env},
        timeout=20,
    )
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    assert (result.returncode, result.stderr) == (0, "")
    return result


def rows(home, sql, params=()):
    db = sqlite3.connect(home / "telemetry.db")
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def hits(home):
    return rows(
        home,
        "select harness, session_id, tool_use_id, event, stage, lesson_id, status_at_injection, trigger_key, "
        "outcome, injected_chars, error_class from hits order by seq",
    )


def claude_failure(repo, session="s1", tool_use_id="tu1", error=ERROR):
    return {
        "session_id": session,
        "transcript_path": "/t.jsonl",
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "tool_use_id": tool_use_id,
        "error": error,
        "is_interrupt": False,
    }


def session_start(repo, source, session="s1", hook_event_name="SessionStart", model="claude-opus-5"):
    return {
        "session_id": session,
        "cwd": str(repo),
        "hook_event_name": hook_event_name,
        "source": source,
        "model": model,
    }


def pre_tool(cwd, command=None, *, tool="Bash", file_path=None, session="s1"):
    tool_input = {"command": command} if command is not None else {"file_path": file_path, "content": "x"}
    return {
        "session_id": session,
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "tu-pre",
    }


def context(event_name, text):
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


# ---------------------------------------------------------------- end to end


def test_claude_repair_shows_the_lesson_records_it_and_dedupes_until_compaction(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    text = lesson_text(lesson_id)
    key = f"error:repair:{SIGNATURE}"

    first = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
    assert json.loads(first.stdout) == context("PostToolUseFailure", text)
    assert first.stdout == json.dumps(context("PostToolUseFailure", text))
    assert hits(home) == [
        (
            "claude",
            "s1",
            "tu1",
            "tool_failed",
            "repair",
            lesson_id,
            "provisional",
            key,
            "shown",
            len(text),
            None,
        )
    ]
    assert rows(home, "select session_id, context_epoch, lesson_id, trigger_key from shown") == [
        ("s1", 0, lesson_id, key)
    ]
    assert rows(home, "select cwd, repo_id, common_dir from cwd_repo") == [
        (str(repo), REPO, common_dir(repo))
    ]

    second = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, tool_use_id="tu2"))
    assert second.stdout == ""
    assert hits(home)[-1] == (
        "claude",
        "s1",
        "tu2",
        "tool_failed",
        "repair",
        None,
        None,
        None,
        "no_match",
        0,
        None,
    )

    compact = run_hook(home, "claude", "SessionStart", session_start(repo, "compact"))
    assert compact.stdout == ""
    assert rows(home, "select session_id, context_epoch from session_epoch") == [("s1", 1)]

    third = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, tool_use_id="tu3"))
    assert json.loads(third.stdout) == context("PostToolUseFailure", text)
    assert rows(home, "select context_epoch, lesson_id from shown order by context_epoch") == [
        (0, lesson_id),
        (1, lesson_id),
    ]


def test_other_sessions_and_unrelated_errors_are_not_deduped_against_each_other(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    assert run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)).stdout != ""
    other = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, session="s2"))
    assert json.loads(other.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
    unrelated = run_hook(
        home, "claude", "PostToolUseFailure", claude_failure(repo, "s3", error="Exit code 2\nboom")
    )
    assert unrelated.stdout == ""


def test_session_start_injects_pinned_lessons_and_tells_the_human_about_waiting_candidates(env):
    home, repo = env
    db = seed_store(home, repo)
    pinned = add_lesson(db, fingerprint="fp1", triggers=REPAIR, status="pinned")
    add_lesson(db, fingerprint="fp2", triggers=REPAIR, status="promoted")
    store.create_lesson(
        db,
        text=LessonText("make", "run `make all`", "make: no rule"),
        scope=RepoScope(REPO),
        trust_class=TrustClass.OWN_CODE,
        triggers=REPAIR,
        fingerprint="fp3",
        actor="miner",
    )
    text = lesson_text(pinned, reviewed="reviewed 2027-01-15")

    start = run_hook(home, "claude", "SessionStart", session_start(repo, "startup"))
    assert json.loads(start.stdout) == {
        "systemMessage": "1 scars lesson waiting: run scars inbox",
        **context("SessionStart", text),
    }
    assert hits(home) == [
        (
            "claude",
            "s1",
            None,
            "session_start",
            "plan",
            pinned,
            "promoted",
            "start:plan",
            "shown",
            len(text),
            None,
        )
    ]

    resumed = run_hook(home, "claude", "SessionStart", session_start(repo, "resume"))
    assert json.loads(resumed.stdout) == {"systemMessage": "1 scars lesson waiting: run scars inbox"}

    compact = run_hook(home, "claude", "SessionStart", session_start(repo, "compact"))
    assert json.loads(compact.stdout) == context("SessionStart", text)
    assert rows(home, "select context_epoch, lesson_id, trigger_key from shown order by context_epoch") == [
        (0, pinned, "start:plan"),
        (1, pinned, "start:plan"),
    ]


def test_claude_post_compact_prints_nothing_and_leaves_the_epoch_alone(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR, status="pinned")
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PostCompact", "trigger": "auto"}
    assert run_hook(home, "claude", "PostCompact", payload).stdout == ""
    assert not (home / "telemetry.db").exists()


def test_user_prompt_submit_delivers_a_pinned_prompt_lesson(env):
    home, repo = env
    trigger = (Trigger(TriggerKind.PROMPT, "express module", Stage.PLAN),)
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=trigger, status="pinned")
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "UserPromptSubmit"}
    shown = run_hook(
        home, "claude", "UserPromptSubmit", {**payload, "prompt": "the Express module is missing"}
    )
    assert json.loads(shown.stdout) == context(
        "UserPromptSubmit", lesson_text(lesson_id, reviewed="reviewed 2027-01-15")
    )
    assert (
        run_hook(home, "codex", "UserPromptSubmit", {**payload, "prompt": "fix the express module"}).stdout
        == ""
    )


def test_codex_post_tool_use_with_a_nonzero_exit_delivers_repair(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    payload = {
        "session_id": "thr_1",
        "transcript_path": None,
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "model": "gpt-5.5-codex",
        "permission_mode": "default",
        "turn_id": "turn_1",
        "tool_name": "Bash",
        "tool_use_id": "call_1",
        "tool_input": {"command": "npm test"},
        "tool_response": "Wall time: 0.5 seconds\nProcess exited with code 1\nOutput:\n" + SIGNATURE,
    }
    result = run_hook(home, "codex", "PostToolUse", payload)
    assert json.loads(result.stdout) == context("PostToolUse", lesson_text(lesson_id))
    assert rows(home, "select harness, model_id, event, outcome from hits") == [
        ("codex", "gpt-5.5-codex", "tool_failed", "shown")
    ]
    success = {**payload, "tool_use_id": "call_2", "tool_response": "all good"}
    assert run_hook(home, "codex", "PostToolUse", success).stdout == ""
    assert len(hits(home)) == 1


def test_codex_session_start_compact_reinjects_pinned_lessons(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR, status="pinned")
    text = lesson_text(lesson_id, reviewed="reviewed 2027-01-15")
    start = session_start(repo, "startup", session="thr_1", model="gpt-5.5-codex")
    assert json.loads(run_hook(home, "codex", "SessionStart", start).stdout) == context("SessionStart", text)
    again = run_hook(home, "codex", "SessionStart", {**start, "source": "resume"})
    assert again.stdout == ""
    compact = run_hook(home, "codex", "SessionStart", {**start, "source": "compact"})
    assert json.loads(compact.stdout) == context("SessionStart", text)


def test_pre_tool_use_in_phase_1_is_silent_for_an_ordinary_command(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    for harness in ("claude", "codex"):
        assert run_hook(home, harness, "PreToolUse", pre_tool(repo, "npm test")).stdout == ""
    assert [h[8] for h in hits(home)] == ["no_match", "no_match"]


# ---------------------------------------------------------------- agent deny rule


@pytest.mark.parametrize(
    ("harness", "command", "denied"),
    [
        ("claude", "scars promote L-1a2b3c4d", "promote"),
        ("codex", "uv run scars pin L-1a2b3c4d", "pin"),
        ("claude", "cd /tmp && bash -c 'scars off'", "off"),
        ("claude", "python3 -m scars level L-1 warn", "level"),
        ("codex", "~/.scars/versions/0.1/bin/scars inbox", "inbox"),
        ("claude", "scars $SUB L-1", "<unknown>"),
    ],
)
def test_agent_shell_calls_to_privileged_scars_commands_are_denied(env, harness, command, denied):
    home, repo = env
    result = run_hook(home, harness, "PreToolUse", pre_tool(repo, command))
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"scars: agents may not run `scars {denied}`. "
            "It changes which lessons reach agents, so a human runs it at a terminal.",
        }
    }
    assert hits(home)[-1][3:9] == ("pre_tool", "gate", None, None, "agent_deny", "blocked")


def test_unparseable_command_that_mentions_scars_is_denied_and_others_stay_silent(env):
    home, repo = env
    denied = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "echo $(scars promote L-1)"))
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "scars: this command mentions scars but cannot be parsed"
    )
    assert run_hook(home, "claude", "PreToolUse", pre_tool(repo, "echo $(date)")).stdout == ""


@pytest.mark.parametrize(
    "command",
    ["scars ls", "scars why L-1", "git commit -m 'scars promote'", "scars --version", "scars hook claude x"],
)
def test_reading_scars_commands_and_mere_mentions_are_allowed(env, command):
    home, repo = env
    assert run_hook(home, "claude", "PreToolUse", pre_tool(repo, command)).stdout == ""


def test_agent_edits_under_the_scars_home_are_denied_for_both_harnesses(env):
    home, repo = env
    reason = (
        "scars: agents may not edit files under the scars home directory. Lessons change only through scars "
        "commands a human runs at a terminal."
    )
    write = pre_tool(repo, tool="Write", file_path=str(home / "lessons.db"))
    assert json.loads(run_hook(home, "claude", "PreToolUse", write).stdout)["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    patch = f"*** Begin Patch\n*** Add File: {home}/OFF\n+x\n*** End Patch\n"
    codex_edit = {**pre_tool(repo, patch), "tool_name": "apply_patch"}
    assert (
        json.loads(run_hook(home, "codex", "PreToolUse", codex_edit).stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        == reason
    )
    elsewhere = pre_tool(repo, tool="Edit", file_path=str(repo / "a.py"))
    assert run_hook(home, "claude", "PreToolUse", elsewhere).stdout == ""


def test_edit_through_a_relative_path_or_symlink_into_the_home_is_denied(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    link = tmp_path / "link"
    link.symlink_to(home)
    for file_path, cwd in (("../home/OFF", tmp_path / "repo"), (str(link / "OFF"), tmp_path)):
        event = claude.parse("PreToolUse", pre_tool(cwd, tool="Write", file_path=file_path))
        assert hook.agent_deny_reason(event, home) is not None
    sibling = claude.parse(
        "PreToolUse", pre_tool(tmp_path, tool="Write", file_path=str(tmp_path / "home2/x"))
    )
    assert hook.agent_deny_reason(sibling, home) is None


def test_the_deny_rule_works_even_when_the_lessons_store_is_missing(env):
    home, repo = env
    result = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "scars retire L-1"))
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- faults


def assert_silent_and_fast(result):
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert result.elapsed_ms < 250, f"took {result.elapsed_ms:.0f} ms"


def test_missing_lessons_db_prints_nothing_quickly_and_records_an_error(env):
    home, repo = env
    assert_silent_and_fast(run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)))
    assert [(h[8], h[10]) for h in hits(home)] == [("error", "lessons_unavailable")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_lessons_db_prints_nothing_quickly(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, session="warm"))
    os.chmod(home / "lessons.db", 0)
    try:
        assert_silent_and_fast(run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)))
    finally:
        os.chmod(home / "lessons.db", 0o600)
    assert hits(home)[-1][8:] == ("error", 0, "lessons_unavailable")


def test_garbage_lessons_db_prints_nothing_quickly(env):
    home, repo = env
    (home / "lessons.db").write_bytes(b"this is not a database at all\n" * 200)
    assert_silent_and_fast(run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)))
    assert hits(home)[-1][8:] == ("error", 0, "lessons_unavailable")


def test_locked_telemetry_still_delivers_the_lesson_and_drops_the_hit(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    run_hook(home, "claude", "SessionStart", session_start(repo, "startup", session="warm"))
    before = hits(home)
    locker = sqlite3.connect(home / "telemetry.db", isolation_level=None)
    locker.execute("begin immediate")
    locker.execute("insert into session_epoch (session_id, context_epoch) values ('other', 0)")
    try:
        result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
        assert json.loads(result.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
        assert result.elapsed_ms < 250
    finally:
        locker.execute("rollback")
        locker.close()
    assert hits(home) == before
    assert rows(home, "select count(*) from shown where session_id = 's1'") == [(0,)]


def test_match_deadline_is_recorded(env, monkeypatch):
    home, repo = env
    db = seed_store(home, repo)
    for i in range(5):
        add_lesson(
            db, fingerprint=f"fp{i}", triggers=(Trigger(TriggerKind.ERROR, f"other error {i}", Stage.REPAIR),)
        )
    monkeypatch.setenv("SCARS_HOME", str(home))
    monkeypatch.setattr(hook, "MATCH_DEADLINE_MS", 0)
    out = io.StringIO()
    hook.run(Harness.CLAUDE, "PostToolUseFailure", io.StringIO(json.dumps(claude_failure(repo))), out)
    assert out.getvalue() == ""
    assert [(h[8], h[10]) for h in hits(home)] == [("no_match", "match_deadline")]


@pytest.mark.parametrize("stdin", ["not json", "[]", '{"hook_event_name": "PostToolUseFailure"}', ""])
def test_malformed_stdin_prints_nothing_and_logs_the_cause(env, stdin):
    home, _repo = env
    result = run_hook(home, "claude", "PostToolUseFailure", stdin)
    assert result.stdout == ""
    assert "MalformedInput" in (home / "logs" / "hook.log").read_text()
    assert [(h[0], h[1], h[3], h[8], h[10]) for h in hits(home)] == [
        ("claude", "", "unknown", "error", "malformed_input")
    ]


def test_without_debug_nothing_is_logged(env):
    home, _repo = env
    result = run_hook(home, "claude", "PostToolUseFailure", "not json", SCARS_DEBUG="")
    assert result.stdout == ""
    assert not (home / "logs").exists()


def test_unknown_harness_event_prints_nothing(env):
    home, _repo = env
    assert (
        run_hook(home, "claude", "Stop", {"session_id": "s", "cwd": "/", "hook_event_name": "Stop"}).stdout
        == ""
    )


def test_kill_switch_file_skips_all_work(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    (home / "OFF").touch()
    result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
    assert result.stdout == ""
    assert not (home / "telemetry.db").exists()
    assert not (home / "logs").exists()


def test_kill_switch_env_var_skips_all_work(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    result = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "scars promote L-1"), SCARS_OFF="1")
    assert result.stdout == ""
    assert not (home / "telemetry.db").exists()


def test_cwd_outside_any_repo_sees_no_repo_lessons(env, tmp_path):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert run_hook(home, "claude", "PostToolUseFailure", claude_failure(outside)).stdout == ""
    assert rows(home, "select repo_id, common_dir from cwd_repo") == [(None, None)]


def test_a_cached_cwd_skips_git(env, monkeypatch):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    run_hook(home, "claude", "SessionStart", session_start(repo, "startup", session="warm"))
    monkeypatch.setenv("SCARS_HOME", str(home))
    monkeypatch.setattr(hook, "git_common_dir", lambda cwd: pytest.fail("git ran despite a fresh cache"))
    out = io.StringIO()
    hook.run(Harness.CLAUDE, "PostToolUseFailure", io.StringIO(json.dumps(claude_failure(repo))), out)
    assert json.loads(out.getvalue()) == context("PostToolUseFailure", lesson_text(lesson_id))


# ---------------------------------------------------------------- latency


def test_latency_with_1000_lessons(env):
    home, repo = env
    db = seed_store(home, repo)
    for i in range(999):
        add_lesson(
            db,
            fingerprint=f"bulk{i}",
            triggers=(Trigger(TriggerKind.ERROR, f"Error: bulk failure number {i}", Stage.REPAIR),),
            reason=f"Error: bulk failure number {i}",
        )
    add_lesson(db, fingerprint="fp-hit", triggers=REPAIR)
    db.close()
    run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, session="warm"))

    def sample(error):
        times = []
        for i in range(20):
            result = run_hook(
                home, "claude", "PostToolUseFailure", claude_failure(repo, f"lat{i}{error[:4]}", error=error)
            )
            times.append(result.elapsed_ms)
        times.sort()
        return times[9], times[-1]

    no_match = sample("Exit code 1\nError: nothing like the lessons")
    matched = sample(ERROR)
    print("\nhook latency, 1,000 lessons, 20 fresh processes each (ms, process start to exit):")
    print(f"  no match: p50 {no_match[0]:.1f}, p99 {no_match[1]:.1f}")
    print(f"  match:    p50 {matched[0]:.1f}, p99 {matched[1]:.1f}")
    assert no_match[1] < 1000 and matched[1] < 1000
