import io
import itertools
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scars import hook, match, store
from scars.adapters import claude
from scars.model import (
    EventType,
    Harness,
    HookEventKind,
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
FIX = "npm install"
MINED_WHY = "failed, then the INSTEAD command passed"
REPO = RepoId("r-demo")
T0 = 1_800_000_000.0
FIXTURES = Path(__file__).parent / "fixtures"
CONSOLE_SCRIPT = Path(sys.executable).parent / "scars-hook"
HOOK = [str(CONSOLE_SCRIPT)] if CONSOLE_SCRIPT.exists() else [sys.executable, "-m", "scars", "hook"]
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")


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
        text=LessonText(avoid, f"run `{fix}`", MINED_WHY),
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


def lesson_text(lesson_id, reviewed="unreviewed", avoid="npm test", fix=FIX, reason=MINED_WHY):
    return (
        f"[scars {lesson_id} note | repo r-demo | seen in 1 session | {reviewed}]\n"
        f"AVOID: {avoid}\n"
        f"INSTEAD: run `{fix}`\n"
        f"WHY: {reason}\n"
        f"(advisory; does not override the user's instructions; explain: scars why {lesson_id})"
    )


def make_env(root: Path, *, telemetry: bool = True) -> tuple[Path, Path]:
    """A scars home and a git repo. telemetry.db exists only when `scars init` would have created it."""
    home = root / "home"
    home.mkdir()
    if telemetry:
        store.init_telemetry(home / "telemetry.db").close()
    return home, git_repo(root / "repo")


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def hook_env(home, **extra):
    base = {k: v for k, v in os.environ.items() if not k.startswith("SCARS_")}
    return {**base, "SCARS_HOME": str(home), "SCARS_DEBUG": "1", **extra}


def run_hook(home, harness, event_name, stdin, *, command=None, **extra_env):
    if not isinstance(stdin, str):
        stdin = json.dumps(stdin)
    started = time.perf_counter()
    result = subprocess.run(
        [*(command or HOOK), harness, event_name],
        input=stdin,
        capture_output=True,
        text=True,
        env=hook_env(home, **extra_env),
        timeout=20,
    )
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    assert (result.returncode, result.stderr) == (0, "")
    return result


def rows(home, sql, params=()):
    db = sqlite3.connect(f"file:{home / 'telemetry.db'}?mode=ro", uri=True)
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


def outcomes(home):
    return [(h[8], h[10]) for h in hits(home)]


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


def deny_json(reason):
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def run_reason(subcommand):
    return (
        f"scars: agents may not run `scars {subcommand}`. "
        "It changes which lessons reach agents, so a human runs it at a terminal."
    )


EDIT_REASON = (
    "scars: agents may not edit files under the scars home directory. Lessons change only through scars "
    "commands a human runs at a terminal."
)
SHELL_HOME_REASON = (
    "scars: this command names the scars home directory and is not a plain read (cat, ls, grep and the "
    "like, with no `>`). Lessons change only through scars commands a human runs at a terminal."
)
GATE_NO_MATCH = ("pre_tool", "gate", None, None, None, "no_match", 0, None)
GATE_DENY = ("pre_tool", "gate", None, None, "agent_deny", "blocked", 0, None)


# ---------------------------------------------------------------- end to end


def test_claude_repair_shows_the_lesson_records_it_and_dedupes_until_compaction(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    text = lesson_text(lesson_id)
    key = f"error:repair:{SIGNATURE}"

    first = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
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
    assert hits(home)[-1][3:9] == ("compact", "plan", None, None, None, "no_match")

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
    assert [(h[1], h[5], h[8]) for h in hits(home)] == [
        ("s1", lesson_id, "shown"),
        ("s2", lesson_id, "shown"),
        ("s3", None, "no_match"),
    ]


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


def test_a_forked_session_starts_with_the_pinned_lessons(env):
    home, repo = env
    pinned = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR, status="pinned")
    text = lesson_text(pinned, reviewed="reviewed 2027-01-15")
    for harness in ("claude", "codex"):
        fork = session_start(repo, "fork", session=f"fork-{harness}")
        assert json.loads(run_hook(home, harness, "SessionStart", fork).stdout) == context(
            "SessionStart", text
        )
    assert [(h[0], h[3], h[5], h[8]) for h in hits(home)] == [
        ("claude", "session_start", pinned, "shown"),
        ("codex", "session_start", pinned, "shown"),
    ]


def test_claude_post_compact_prints_nothing_and_leaves_the_epoch_alone(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR, status="pinned")
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PostCompact", "trigger": "auto"}
    assert run_hook(home, "claude", "PostCompact", payload).stdout == ""
    assert hits(home) == []
    assert rows(home, "select * from session_epoch") == []
    # The same session's SessionStart compact is what moves the epoch and records a hit.
    assert run_hook(home, "claude", "SessionStart", session_start(repo, "compact")).stdout != ""
    assert rows(home, "select session_id, context_epoch from session_epoch") == [("s1", 1)]
    assert [h[3] for h in hits(home)] == ["compact"]


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
    assert [(h[0], h[3], h[4], h[5], h[8]) for h in hits(home)] == [
        ("claude", "prompt", "plan", lesson_id, "shown"),
        ("codex", "prompt", "plan", None, "no_match"),
    ]


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
    assert [(h[3], h[8]) for h in hits(home)] == [
        ("session_start", "shown"),
        ("session_start", "no_match"),
        ("compact", "shown"),
    ]


def test_pre_tool_use_in_phase_1_is_silent_for_an_ordinary_command(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    for harness in ("claude", "codex"):
        assert run_hook(home, harness, "PreToolUse", pre_tool(repo, "npm test")).stdout == ""
    assert [h[0] for h in hits(home)] == ["claude", "codex"]
    assert {h[3:] for h in hits(home)} == {GATE_NO_MATCH}


def test_a_promoted_block_lesson_never_denies_in_phase_1(env):
    home, repo = env
    db = seed_store(home, repo)
    gate = (
        Trigger(TriggerKind.COMMAND, "npm test", Stage.GATE),
        Trigger(TriggerKind.PATH, "src/**", Stage.GATE),
    )
    lesson_id = add_lesson(db, fingerprint="fp-block", triggers=gate, status="promoted")
    db.execute("update lessons set level = 'block' where id = ?", (lesson_id,))
    db.close()
    assert run_hook(home, "claude", "PreToolUse", pre_tool(repo, "npm test")).stdout == ""
    edit = pre_tool(repo, tool="Write", file_path=str(repo / "src" / "a.py"))
    assert run_hook(home, "claude", "PreToolUse", edit).stdout == ""
    assert [h[3:] for h in hits(home)] == [GATE_NO_MATCH, GATE_NO_MATCH]


# ---------------------------------------------------------------- agent deny rule


@pytest.mark.parametrize(
    ("harness", "command", "reason"),
    [
        ("claude", "scars promote L-1a2b3c4d", run_reason("promote")),
        ("codex", "uv run scars pin L-1", run_reason("pin")),
        ("claude", "cd /tmp && bash -c 'scars off'", run_reason("off")),
        ("claude", "bash -c \"sh -c 'scars off'\"", run_reason("off")),
        ("claude", "'sc''ars' off", run_reason("off")),
        ("claude", 'sc"ars" mute L-1', run_reason("mute")),
        ("claude", "python3 -m scars level L-1 warn", run_reason("level")),
        ("codex", "~/.scars/versions/0.1/bin/scars inbox", run_reason("inbox")),
        ("claude", "/usr/local/bin/scars retire L-1", run_reason("retire")),
        ("claude", "./.venv/bin/scars unpin L-1", run_reason("unpin")),
        ("claude", "echo $(scars promote L-1)", run_reason("promote")),
        ("claude", "echo " + "a" * 4096 + " && scars promote L-1", run_reason("promote")),
        ("claude", "touch ~/.scars/OFF", SHELL_HOME_REASON),
        ("codex", "echo a > ~/.scars/x", SHELL_HOME_REASON),
        ("claude", "cp evil.db $HOME/.scars/lessons.db", SHELL_HOME_REASON),
        ("claude", "rm ${HOME}/.scars/OFF", SHELL_HOME_REASON),
        ("claude", "rm $SCARS_HOME/OFF", SHELL_HOME_REASON),
        ("claude", "rm -f ${SCARS_HOME}/OFF", SHELL_HOME_REASON),
        ("claude", 'touch ~/".scars"/OFF', SHELL_HOME_REASON),
        ("claude", "cat ~/.scars/lessons.db > /tmp/copy", SHELL_HOME_REASON),
        ("claude", "find ~/.scars -name '*.db' -delete", SHELL_HOME_REASON),
        ("claude", "find ~/.scars -exec rm {} +", SHELL_HOME_REASON),
        ("claude", "cat $(echo ~/.scars/lessons.db)", SHELL_HOME_REASON),
    ],
)
def test_agent_shell_calls_that_could_change_scars_are_denied(env, harness, command, reason):
    home, repo = env
    result = run_hook(home, harness, "PreToolUse", pre_tool(repo, command))
    assert result.stdout == deny_json(reason)
    assert [h[3:] for h in hits(home)] == [GATE_DENY]


def test_a_command_naming_the_absolute_scars_home_is_denied_unless_it_only_reads(env):
    home, repo = env
    denied = run_hook(home, "claude", "PreToolUse", pre_tool(repo, f"rm {home}/OFF"))
    assert denied.stdout == deny_json(SHELL_HOME_REASON)
    read = run_hook(home, "claude", "PreToolUse", pre_tool(repo, f"ls -la {home} && wc -l {home}/logs/x"))
    assert read.stdout == ""
    assert [h[3:] for h in hits(home)] == [GATE_DENY, GATE_NO_MATCH]


@pytest.mark.parametrize(
    "command",
    [
        "scars ls",
        "scars why L-1",
        "scars --version",
        "scars hook claude x",
        "scars doctor",
        "grep scars $(printf README.md)",
        "echo hello scars",
        "echo $(date)",
        "cat ~/.scars/lessons.db",
        "ls -la ~/.scars | head -n 3",
        "grep -r promote ~/.scars/logs",
        "grep -rn retire $HOME/.scars/logs",
        "rg -n retire ~/.scars/logs",
        'grep "a > b" ~/.scars/x',
        "find ~/.scars -name '*.db'",
        "git log --grep scars",
        "npm run scarsify off",
    ],
)
def test_reading_scars_commands_and_mere_mentions_are_allowed(env, command):
    home, repo = env
    assert run_hook(home, "claude", "PreToolUse", pre_tool(repo, command)).stdout == ""
    assert [h[3:] for h in hits(home)] == [GATE_NO_MATCH]


def test_quoted_text_that_reads_as_a_scars_invocation_is_denied(env):
    # The rule is one regex over the raw text, so it cannot tell a commit message from a nested script.
    home, repo = env
    result = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "git commit -m 'scars promote'"))
    assert result.stdout == deny_json(run_reason("promote"))


def test_agent_edits_under_the_scars_home_are_denied_for_both_harnesses(env):
    home, repo = env
    write = pre_tool(repo, tool="Write", file_path=str(home / "lessons.db"))
    assert run_hook(home, "claude", "PreToolUse", write).stdout == deny_json(EDIT_REASON)
    patch = f"*** Begin Patch\n*** Add File: {home}/OFF\n+x\n*** End Patch\n"
    codex_edit = {**pre_tool(repo, patch), "tool_name": "apply_patch"}
    assert run_hook(home, "codex", "PreToolUse", codex_edit).stdout == deny_json(EDIT_REASON)
    elsewhere = pre_tool(repo, tool="Edit", file_path=str(repo / "a.py"))
    assert run_hook(home, "claude", "PreToolUse", elsewhere).stdout == ""
    assert [h[3:] for h in hits(home)] == [GATE_DENY, GATE_DENY, GATE_NO_MATCH]


def test_edit_through_a_relative_path_or_symlink_into_the_home_is_denied(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    link = tmp_path / "link"
    link.symlink_to(home)
    for file_path, cwd in (("../home/OFF", tmp_path / "repo"), (str(link / "OFF"), tmp_path)):
        event = claude.parse("PreToolUse", pre_tool(cwd, tool="Write", file_path=file_path))
        assert hook.agent_deny_reason(event, home) == EDIT_REASON
    sibling = claude.parse(
        "PreToolUse", pre_tool(tmp_path, tool="Write", file_path=str(tmp_path / "home2/x"))
    )
    assert hook.agent_deny_reason(sibling, home) is None


def test_the_deny_rule_works_even_when_the_lessons_store_is_missing(env):
    home, repo = env
    result = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "scars retire L-1"))
    assert result.stdout == deny_json(run_reason("retire"))
    assert [h[3:] for h in hits(home)] == [GATE_DENY]


# ---------------------------------------------------------------- stdin


def popen_hook(home, harness, event_name):
    return subprocess.Popen(
        [*HOOK, harness, event_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook_env(home),
    )


def finish(proc, started):
    """Wait for the hook without closing its stdin, as a harness that keeps the pipe open would."""
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("the hook waited on an open stdin pipe")
    elapsed_ms = (time.perf_counter() - started) * 1000
    out, err = proc.stdout.read(), proc.stderr.read()
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()
    assert (proc.returncode, err) == (0, "")
    return out, elapsed_ms


def test_a_complete_payload_is_answered_while_the_writer_keeps_stdin_open(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    proc = popen_hook(home, "claude", "PostToolUseFailure")
    started = time.perf_counter()
    proc.stdin.write(json.dumps(claude_failure(repo)))
    proc.stdin.flush()
    out, elapsed_ms = finish(proc, started)
    assert out == json.dumps(context("PostToolUseFailure", lesson_text(lesson_id)))
    assert elapsed_ms < 1000, f"took {elapsed_ms:.0f} ms"
    assert outcomes(home) == [("shown", None)]


@pytest.mark.parametrize("sent", ["", '{"session_id": "s1", "cwd": '])
def test_a_writer_that_never_finishes_gets_no_answer_after_the_deadline(env, sent):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    proc = popen_hook(home, "claude", "PostToolUseFailure")
    started = time.perf_counter()
    proc.stdin.write(sent)
    proc.stdin.flush()
    out, elapsed_ms = finish(proc, started)
    assert out == ""
    assert hook.STDIN_DEADLINE_MS <= elapsed_ms < 2000, f"took {elapsed_ms:.0f} ms"
    assert [(h[0], h[1], h[3], h[8], h[10]) for h in hits(home)] == [
        ("claude", "", "unknown", "error", "stdin_timeout")
    ]


def test_an_oversized_payload_gets_no_answer(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    big = {**claude_failure(repo), "padding": "x" * hook.STDIN_LIMIT_BYTES}
    assert run_hook(home, "claude", "PostToolUseFailure", big).stdout == ""
    assert outcomes(home) == [("error", "stdin_oversized")]


@pytest.mark.parametrize("stdin", ["[" * 100_000 + "]" * 100_000, '{"a":' * 100_000 + "1" + "}" * 100_000])
def test_deeply_nested_json_is_malformed_input(env, stdin):
    home, _repo = env
    assert run_hook(home, "claude", "PostToolUseFailure", stdin).stdout == ""
    assert outcomes(home) == [("error", "malformed_input")]


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
    assert outcomes(home) == [("error", "malformed_input")]


def test_unknown_harness_event_prints_nothing(env):
    home, _repo = env
    stop = {"session_id": "s", "cwd": "/", "hook_event_name": "Stop"}
    assert run_hook(home, "claude", "Stop", stop).stdout == ""
    assert outcomes(home) == [("error", "malformed_input")]


# ---------------------------------------------------------------- fixtures end to end

NO_LESSON = (None, None, None, "no_match", 0, None)
STAGES = {
    "session_start": "plan",
    "compact": "plan",
    "prompt": "plan",
    "pre_tool": "gate",
    "tool_failed": "repair",
}


def fixture_cases():
    for path in sorted(FIXTURES.glob("*/*/*.json")):
        harness, version, event_name = path.parts[-3], path.parts[-2], path.stem
        for case in json.loads(path.read_text())["cases"]:
            yield pytest.param(
                harness, event_name, case, id=f"{harness}/{version}/{event_name}/{case['name']}"
            )


@pytest.mark.parametrize(("harness", "event_name", "case"), list(fixture_cases()))
def test_every_fixture_replays_through_the_hook(tmp_path, harness, event_name, case):
    home, _repo = make_env(tmp_path)
    store.init_lessons(home / "lessons.db").close()
    result = run_hook(home, harness, event_name, case["stdin"])
    assert result.stdout == ""
    if "event" in case:
        e = case["event"]
        assert hits(home) == [
            (harness, e["session_id"], e["tool_use_id"], e["event"], STAGES[e["event"]], *NO_LESSON)
        ]
    elif case["error"] == "Ignored":
        assert hits(home) == []
    else:
        assert [(h[0], h[3], h[8], h[10]) for h in hits(home)] == [
            (harness, "unknown", "error", "malformed_input")
        ]


def test_the_hook_stage_tables_agree_with_the_matcher():
    expected = {k: r.stage for k, r in match.STAGE_RULES.items()}
    assert expected == hook.STAGES
    assert set(hook.STAGES) == set(HookEventKind)
    for kind, query in hook.QUERIES.items():
        rule = match.STAGE_RULES[kind]
        assert (query.stage, query.pinned_only, query.trigger_kinds) == (
            rule.stage,
            rule.pinned_only,
            rule.trigger_kinds,
        )
    assert set(hook.QUERIES) == set(HookEventKind) - {HookEventKind.PRE_TOOL}


# ---------------------------------------------------------------- telemetry


def test_every_hits_column_is_written_for_a_shown_and_a_no_match_hit(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    text = lesson_text(lesson_id)
    before = time.time()
    run_hook(
        home, "claude", "PostToolUseFailure", {**claude_failure(repo), "model": ""}, SCARS_ARM="treatment"
    )
    miss = {**claude_failure(repo, "s2", "tu2", "Exit code 2\nboom"), "model": "claude-opus-5"}
    run_hook(home, "claude", "PostToolUseFailure", miss, SCARS_ARM="")
    after = time.time()
    columns = [c[1] for c in rows(home, "pragma table_info(hits)")]
    assert columns == [
        "seq",
        "at",
        "harness",
        "harness_version",
        "model_id",
        "session_id",
        "tool_use_id",
        "event",
        "stage",
        "lesson_id",
        "lesson_revision",
        "status_at_injection",
        "trigger_key",
        "outcome",
        "injected_chars",
        "latency_ms",
        "error_class",
        "arm",
    ]
    got = rows(home, "select * from hits order by seq")
    for row in got:
        assert before <= row[1] <= after
        assert isinstance(row[15], float) and 0 < row[15] < 1000
    assert [r[:1] + r[2:15] + r[16:] for r in got] == [
        (
            1,
            "claude",
            None,
            None,
            "s1",
            "tu1",
            "tool_failed",
            "repair",
            lesson_id,
            2,
            "provisional",
            f"error:repair:{SIGNATURE}",
            "shown",
            len(text),
            None,
            "treatment",
        ),
        (
            2,
            "claude",
            None,
            "claude-opus-5",
            "s2",
            "tu2",
            "tool_failed",
            "repair",
            None,
            None,
            None,
            None,
            "no_match",
            0,
            None,
            None,
        ),
    ]


def test_missing_telemetry_db_is_never_created_and_the_lesson_still_arrives(tmp_path):
    home, repo = make_env(tmp_path, telemetry=False)
    db = seed_store(home, repo)
    lesson_id = add_lesson(db, fingerprint="fp1", triggers=REPAIR)
    db.close()
    for tool_use_id in ("tu1", "tu2"):
        result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo, tool_use_id=tool_use_id))
        assert json.loads(result.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
    assert not (home / "telemetry.db").exists()


@NOT_ROOT
def test_a_read_only_home_without_telemetry_still_delivers(tmp_path):
    home, repo = make_env(tmp_path, telemetry=False)
    db = seed_store(home, repo)
    lesson_id = add_lesson(db, fingerprint="fp1", triggers=REPAIR)
    db.close()
    os.chmod(home, 0o500)
    try:
        result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
        denied = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "scars off"))
    finally:
        os.chmod(home, 0o700)
    assert json.loads(result.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
    assert denied.stdout == deny_json(run_reason("off"))
    assert not (home / "telemetry.db").exists()


@NOT_ROOT
@pytest.mark.parametrize("target", ["home", "file"])
def test_a_read_only_home_or_unreadable_telemetry_db_still_delivers(env, target):
    home, repo = env
    db = seed_store(home, repo)
    lesson_id = add_lesson(db, fingerprint="fp1", triggers=REPAIR)
    db.close()
    path, mode, restore = (home, 0o500, 0o700) if target == "home" else (home / "telemetry.db", 0, 0o600)
    os.chmod(path, mode)
    try:
        result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
    finally:
        os.chmod(path, restore)
    assert json.loads(result.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
    assert hits(home) == []


def test_locked_telemetry_still_delivers_the_lesson_and_drops_the_hit(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    run_hook(home, "claude", "SessionStart", session_start(repo, "startup", session="warm"))
    before = hits(home)
    assert len(before) == 1
    locker = sqlite3.connect(home / "telemetry.db", isolation_level=None)
    locker.execute("begin immediate")
    locker.execute("insert into session_epoch (session_id, context_epoch) values ('other', 0)")
    try:
        result = run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo))
        assert json.loads(result.stdout) == context("PostToolUseFailure", lesson_text(lesson_id))
        assert result.elapsed_ms < NO_WAIT_MS
    finally:
        locker.execute("rollback")
        locker.close()
    assert hits(home) == before
    assert rows(home, "select count(*) from shown where session_id = 's1'") == [(0,)]


# ---------------------------------------------------------------- faults

# These runs only prove the hook never waits on a lock, which would cost SQLite's 2 s busy timeout.
# Latency itself is measured by scripts/bench_hook.py; a tight wall-clock bound flakes on shared CI runners.
NO_WAIT_MS = 1000


def assert_silent_and_fast(result):
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert result.elapsed_ms < NO_WAIT_MS, f"took {result.elapsed_ms:.0f} ms"


def test_missing_lessons_db_prints_nothing_quickly_and_records_an_error(env):
    home, repo = env
    assert_silent_and_fast(run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)))
    assert outcomes(home) == [("error", "lessons_unavailable")]


@NOT_ROOT
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


def test_match_deadline_keeps_the_lessons_matched_before_it_and_is_recorded(env, monkeypatch):
    home, repo = env
    db = seed_store(home, repo)
    first = add_lesson(db, fingerprint="fp-first", triggers=REPAIR)
    db.execute("update lessons set sessions_seen = 9 where id = ?", (first,))
    add_lesson(db, fingerprint="fp-second", triggers=REPAIR)
    db.close()
    monkeypatch.setenv("SCARS_HOME", str(home))
    ticks = itertools.count()
    monkeypatch.setattr(hook, "MATCH_CLOCK", lambda: float(next(ticks)))
    out = io.StringIO()
    hook.run(Harness.CLAUDE, "PostToolUseFailure", io.StringIO(json.dumps(claude_failure(repo))), out)
    text = lesson_text(first).replace("seen in 1 session", "seen in 9 sessions")
    assert out.getvalue() == json.dumps(context("PostToolUseFailure", text))
    assert [(h[5], h[8], h[10]) for h in hits(home)] == [(first, "shown", "match_deadline")]


def test_kill_switch_file_skips_all_work(env):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    (home / "OFF").touch()
    assert run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)).stdout == ""
    assert hits(home) == []
    assert not (home / "logs").exists()
    (home / "OFF").unlink()
    assert run_hook(home, "claude", "PostToolUseFailure", claude_failure(repo)).stdout != ""
    assert outcomes(home) == [("shown", None)]


def test_kill_switch_env_var_skips_all_work(env):
    home, repo = env
    payload = pre_tool(repo, "scars promote L-1")
    assert run_hook(home, "claude", "PreToolUse", payload, SCARS_OFF="1").stdout == ""
    assert hits(home) == []
    assert run_hook(home, "claude", "PreToolUse", payload).stdout == deny_json(run_reason("promote"))
    assert [h[3:] for h in hits(home)] == [GATE_DENY]


def test_cwd_outside_any_repo_sees_no_repo_lessons(env, tmp_path):
    home, repo = env
    add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert run_hook(home, "claude", "PostToolUseFailure", claude_failure(outside)).stdout == ""
    assert rows(home, "select repo_id, common_dir from cwd_repo") == [(None, None)]
    assert outcomes(home) == [("no_match", None)]


def test_a_cached_cwd_skips_git(env, monkeypatch):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)
    run_hook(home, "claude", "SessionStart", session_start(repo, "startup", session="warm"))
    monkeypatch.setenv("SCARS_HOME", str(home))
    monkeypatch.setattr(hook, "git_common_dir", lambda cwd: pytest.fail("git ran despite a fresh cache"))
    out = io.StringIO()
    hook.run(Harness.CLAUDE, "PostToolUseFailure", io.StringIO(json.dumps(claude_failure(repo))), out)
    assert json.loads(out.getvalue()) == context("PostToolUseFailure", lesson_text(lesson_id))


# ---------------------------------------------------------------- entry points and latency


def test_the_scars_hook_console_script_is_installed_and_scars_hook_still_works(env):
    home, repo = env
    assert CONSOLE_SCRIPT.exists()
    for command in ([str(CONSOLE_SCRIPT)], [sys.executable, "-m", "scars", "hook"]):
        result = run_hook(home, "claude", "PreToolUse", pre_tool(repo, "scars off"), command=command)
        assert result.stdout == deny_json(run_reason("off"))
    assert len(hits(home)) == 2


def imported_modules(home, harness, event_name, payload, **extra_env):
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "-c",
            "from scars.hook_entry import main; main()",
            harness,
            event_name,
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=hook_env(home, **extra_env),
        timeout=20,
    )
    assert result.returncode == 0
    names = {
        line.split("|")[-1].strip() for line in result.stderr.splitlines() if line.startswith("import time:")
    }
    return names, result.stdout


HEAVY = {"argparse", "scars.cli", "scars.match", "scars.render", "scars.shell", "importlib.resources"}


def test_the_hook_entry_imports_only_what_the_path_needs(env):
    home, repo = env
    lesson_id = add_lesson(seed_store(home, repo), fingerprint="fp1", triggers=REPAIR)

    names, out = imported_modules(home, "claude", "PostToolUseFailure", claude_failure(repo), SCARS_OFF="1")
    assert out == "" and "scars.hook" not in names and "scars.model" not in names

    miss = claude_failure(repo, "s2", error="Exit code 2\nboom")
    names, out = imported_modules(home, "claude", "PostToolUseFailure", miss)
    assert out == "" and "scars.hook" in names
    assert names & HEAVY == set()

    names, out = imported_modules(home, "claude", "PreToolUse", pre_tool(repo, "npm test"))
    assert out == "" and names & (HEAVY | {"scars.store"}) == set()

    names, out = imported_modules(home, "claude", "PostToolUseFailure", claude_failure(repo, "s3"))
    assert json.loads(out) == context("PostToolUseFailure", lesson_text(lesson_id))
    assert {"scars.match", "scars.render"} <= names
    assert names & {"argparse", "scars.cli", "importlib.resources"} == set()


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
            assert (result.stdout != "") == (error == ERROR)
            times.append(result.elapsed_ms)
        times.sort()
        return times[9], times[-1]

    no_match = sample("Exit code 1\nError: nothing like the lessons")
    matched = sample(ERROR)
    print("\nhook latency, 1,000 lessons, 20 fresh processes each (ms, process start to exit):")
    print(f"  no match: p50 {no_match[0]:.1f}, p99 {no_match[1]:.1f}")
    print(f"  match:    p50 {matched[0]:.1f}, p99 {matched[1]:.1f}")
    assert no_match[1] < 1000 and matched[1] < 1000
