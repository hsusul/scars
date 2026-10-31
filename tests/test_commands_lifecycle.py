import pytest

from cli_helpers import NOW, connect, human, make_ctx, run, seed
from scars import paths, render, store
from scars.commands import harness_in, refusal
from scars.model import (
    TEXT_CAP,
    EventType,
    LessonText,
    Level,
    MachineScope,
    RepoId,
    RepoScope,
    SessionId,
    SessionScope,
    Stage,
    Status,
    Trigger,
    TriggerKind,
    TrustClass,
)

pytest_plugins = ("cli_fixtures",)

PRIVILEGED_ARGV = [
    ["promote", "L-00000000"],
    ["pin", "L-00000000"],
    ["unpin", "L-00000000"],
    ["level", "L-00000000", "warn"],
    ["retire", "L-00000000"],
    ["mute", "L-00000000"],
    ["off"],
    ["on"],
    ["import-guard", "/nonexistent"],
]


@pytest.mark.parametrize("argv", PRIVILEGED_ARGV, ids=lambda a: a[0])
def test_privileged_command_refuses_when_stdin_is_not_a_terminal(home, user_home, argv):
    ctx = make_ctx(user_home, tty=False)
    assert run(ctx, *argv) == (
        1,
        "",
        f"scars {argv[0]}: refused: stdin and stdout must be a terminal; run it in your own terminal\n",
    )
    assert not paths.kill_switch().exists()


@pytest.mark.parametrize("argv", PRIVILEGED_ARGV, ids=lambda a: a[0])
@pytest.mark.parametrize("variable", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_privileged_command_refuses_when_an_agent_variable_is_set(home, user_home, argv, variable):
    ctx = make_ctx(user_home, env={variable: "1"})
    assert run(ctx, *argv) == (
        1,
        "",
        f"scars {argv[0]}: refused: {variable} is set, so this looks like an agent session; "
        "run it in your own terminal\n",
    )


@pytest.mark.parametrize(
    "ancestor",
    [
        "claude",
        "/Users/u/.local/bin/claude --resume",
        "node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        "codex exec",
        "node /usr/local/lib/node_modules/@openai/codex/bin/codex.js",
    ],
)
def test_privileged_command_refuses_under_a_harness_process(home, user_home, ancestor):
    ctx = make_ctx(user_home, ancestors=["/bin/bash -c scars off", ancestor, "-zsh"])
    assert run(ctx, "off") == (
        1,
        "",
        "scars off: refused: it was started from an agent harness; run it in your own terminal\n",
    )


def test_a_plain_terminal_is_not_refused(user_home):
    assert refusal(make_ctx(user_home)) is None
    assert refusal(make_ctx(user_home, ancestors=["/usr/bin/vim claude.md", "-zsh"])) is None
    assert refusal(make_ctx(user_home, ancestors=["/usr/bin/vim claude.md", "claude", "-zsh"])) == (
        "it was started from an agent harness; run it in your own terminal"
    )
    assert harness_in("/usr/bin/vim claude.md") is False
    assert harness_in("claude --resume") is True


def test_off_creates_the_kill_switch_and_on_removes_it(home, user_home):
    assert run(make_ctx(user_home), "off") == (
        0,
        f"scars is off: every hook does nothing from its next call ({home}/OFF exists). "
        "scars on undoes it.\n",
        "",
    )
    assert (home / "OFF").exists()
    assert paths.is_off() is True
    assert run(make_ctx(user_home), "on") == (
        0,
        "scars is on: hooks consult lessons again from their next call\n",
        "",
    )
    assert not (home / "OFF").exists()
    assert run(make_ctx(user_home), "on") == (0, "scars is already on\n", "")


def test_promote_keeps_the_repo_scope_and_writes_one_human_event(home, user_home):
    lesson_id = seed(home)
    assert run(make_ctx(user_home), "promote", lesson_id) == (
        0,
        f"promoted {lesson_id} (repo app, level note, not pinned)\n",
        "",
    )
    db = connect(home)
    lesson = store.get_lesson(db, lesson_id)
    assert (lesson.status, lesson.scope, lesson.revision) == (Status.PROMOTED, RepoScope("app"), 3)
    assert [(e.type, e.actor, e.at) for e in store.lesson_events(db, lesson_id)][-1] == (
        "promoted",
        "human",
        NOW,
    )
    assert store.verify(db) == []


def test_promote_to_machine_scope(home, user_home):
    lesson_id = seed(home)
    assert run(make_ctx(user_home), "promote", lesson_id, "--machine")[1] == (
        f"promoted {lesson_id} (machine, level note, not pinned)\n"
    )
    assert store.get_lesson(connect(home), lesson_id).scope == MachineScope()


LONG_TEXT = LessonText(
    avoid="starting a second full build while the first one still holds the lock",
    instead=(
        "wait for the first build to finish, or cancel it, "
        "and start the next build only after the lock is gone"
    ),
    reason="two builds that share one derived data folder corrupt each other and both fail with a lock error",
)


def test_promote_refuses_a_scope_whose_header_pushes_the_lesson_over_budget(home, user_home):
    db = connect(home)
    store.add_repo(db, RepoId("r-web-frontend"), "web", "own")
    lesson_id = store.create_lesson(
        db,
        text=LONG_TEXT,
        scope=SessionScope(SessionId("s1")),
        trust_class=TrustClass.HUMAN,
        triggers=[Trigger(TriggerKind.ERROR, "Error: the build lock is held", Stage.REPAIR)],
        fingerprint="fp-long",
        actor="human",
        at=NOW,
    )
    stored = store.get_lesson(db, lesson_id)
    db.close()
    assert max(len(v) for v in (LONG_TEXT.avoid, LONG_TEXT.instead, LONG_TEXT.reason)) <= TEXT_CAP
    assert render.check_budgets(stored, known_commands=frozenset()) is None
    assert run(make_ctx(user_home), "promote", lesson_id, "--repo", "r-web-frontend") == (
        1,
        "",
        f"scars promote: {lesson_id} cannot be promoted to repo r-web-frontend: "
        "the lesson is over the repair budget of 400 characters\n",
    )
    lesson = store.get_lesson(connect(home), lesson_id)
    assert (lesson.status, lesson.revision, lesson.scope) == (Status.CANDIDATE, 1, SessionScope("s1"))


def test_promote_to_an_unknown_repo_is_a_user_error(home, user_home):
    lesson_id = seed(home)
    assert run(make_ctx(user_home), "promote", lesson_id, "--repo", "nope") == (
        1,
        "",
        "scars promote: no registered repo nope\n",
    )


def test_unknown_lesson_is_a_user_error(home, user_home):
    assert run(make_ctx(user_home), "pin", "L-deadbeef") == (1, "", "scars pin: no lesson L-deadbeef\n")


def test_pin_level_unpin_retire_in_order(home, user_home):
    lesson_id = seed(home)
    human(home, lesson_id, EventType.PROMOTED)
    assert run(make_ctx(user_home), "pin", lesson_id) == (0, f"pinned {lesson_id}\n", "")
    assert run(make_ctx(user_home), "level", lesson_id, "warn") == (0, f"{lesson_id} level warn\n", "")
    lesson = store.get_lesson(connect(home), lesson_id)
    assert (lesson.pinned, lesson.level) == (True, Level.WARN)
    assert run(make_ctx(user_home), "unpin", lesson_id) == (0, f"unpinned {lesson_id}\n", "")
    assert run(make_ctx(user_home), "retire", lesson_id) == (0, f"retired {lesson_id}\n", "")
    lesson = store.get_lesson(connect(home), lesson_id)
    assert (lesson.status, lesson.pinned, lesson.level) == (Status.RETIRED, False, Level.NOTE)


def test_level_block_is_refused_in_this_version(home, user_home):
    lesson_id = seed(home)
    human(home, lesson_id, EventType.PROMOTED)
    assert run(make_ctx(user_home), "level", lesson_id, "block") == (
        1,
        "",
        "scars level: level block is not available in this version; use note or warn\n",
    )
    assert store.get_lesson(connect(home), lesson_id).level is Level.NOTE


def test_pin_on_a_provisional_lesson_reports_the_lifecycle_rule(home, user_home):
    lesson_id = seed(home)
    assert run(make_ctx(user_home), "pin", lesson_id) == (
        1,
        "",
        f"scars pin: {lesson_id}: pinned is not allowed from provisional\n",
    )


def test_mute_retires_the_lesson(home, user_home):
    lesson_id = seed(home)
    assert run(make_ctx(user_home), "mute", lesson_id) == (0, f"muted {lesson_id}\n", "")
    assert store.get_lesson(connect(home), lesson_id).status is Status.RETIRED


def test_a_change_between_read_and_write_is_reported_as_a_conflict(home, user_home, monkeypatch):
    lesson_id = seed(home)
    real_get = store.get_lesson

    def stale_then_changed(db, wanted):
        lesson = real_get(db, wanted)
        monkeypatch.setattr(store, "get_lesson", real_get)
        human(home, lesson_id, EventType.PROMOTED)
        return lesson

    monkeypatch.setattr(store, "get_lesson", stale_then_changed)
    assert run(make_ctx(user_home), "pin", lesson_id) == (
        1,
        "",
        f"scars pin: conflict: {lesson_id} is at revision 3, expected 2; "
        "it changed since it was read, so run the command again\n",
    )
