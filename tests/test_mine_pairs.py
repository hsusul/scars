from collections import Counter

import pytest

from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import IDLE_SECONDS, Target, pair_session, target
from scars.model import Harness

START = 1_800_000_000.0
FAIL = "pytest: error: unrecognized arguments: --frobnicate"


def call(tool_use_id, at, *, tool="Bash", command="pytest tests", status=CallStatus.OK, output=""):
    return make_call(
        harness=Harness.CLAUDE,
        session_id="s1",
        tool_use_id=tool_use_id,
        tool=tool,
        raw_input={"command": command},
        command=command if tool == "Bash" else None,
        paths=(),
        status=status,
        output=output,
        at=at,
        cwd="/tmp/app",
        model_id=None,
    )


def failed(tool_use_id, at, **kwargs):
    return call(tool_use_id, at, status=CallStatus.FAILED, output=FAIL, **kwargs)


def outcome(result):
    return (
        [(p.failed.tool_use_id, p.passing.tool_use_id) for p in result.pairs],
        [f.tool_use_id for f in result.still_open],
        result.failures,
        result.unlessonable,
        result.dropped,
    )


def test_a_failure_pairs_with_the_next_pass_of_the_same_program():
    result = pair_session(
        (), [failed("f", START, command="pytest --x tests"), call("p", START + 1)], START + 2
    )
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)


def test_a_failed_edit_call_never_opens():
    edit = failed("f", START, tool="Edit")
    result = pair_session((), [edit, call("r", START + 1, tool="Read")], START + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failed_shell_call_the_parser_rejects_never_opens():
    result = pair_session(
        (), [failed("f", START, command="pytest $("), call("p", START + 1, command="echo $(")], START + 2
    )
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_with_no_lessonable_signature_never_opens():
    timeout = call(
        "f", START, command="pytest --x", status=CallStatus.FAILED, output="Command timed out after 2m 0s"
    )
    result = pair_session((), [timeout, call("p", START + 1)], START + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_idle_for_a_day_is_dropped_by_the_next_call():
    late = call("p", START + IDLE_SECONDS)
    result = pair_session((), [failed("f", START, command="pytest --x"), late], START + IDLE_SECONDS)
    assert outcome(result) == ([], [], 1, 0, 1)


def test_a_pass_on_another_operand_is_skipped_and_the_failure_keeps_waiting():
    stream = [
        failed("f", START, command="git push origin feature"),
        call("other", START + 1, command="git push origin main"),
        call("fix", START + 2, command="git push --set-upstream origin feature"),
    ]
    result = pair_session((), stream, START + 3)
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "git -C app push --force origin main",
            Target(("push",), Counter(["main"]), Counter(["app", "origin"])),
        ),
        (
            "git worktree add -b B upstream/master /p",
            Target(("worktree", "add"), Counter(["upstream/master", "/p"]), Counter(["B"])),
        ),
        ("gh pr diff 1801 --repo o/r", Target(("pr", "diff"), Counter(["1801"]), Counter(["o/r"]))),
        ("npm run build", Target(("run",), Counter(["build"]), Counter())),
        ("xcrun simctl boot 1111-AAAA", Target(("simctl", "boot"), Counter(["1111-AAAA"]), Counter())),
        ("xcodebuild -scheme App build", Target((), Counter(["build"]), Counter(["App"]))),
        ("make -C app test", Target((), Counter(["test"]), Counter(["app"]))),
        ("cat --number -- -notes.md", Target((), Counter(["-notes.md"]), Counter())),
        ("gh api repos/o/r?ref=x", Target(("api", "repos/o/r?ref=x"), Counter(), Counter())),
        ("cat $NOTE", Target((), Counter(["$NOTE"]), Counter())),
    ],
)
def test_the_target_is_the_subcommand_and_positional_operands(command, expected):
    assert target(command) == expected


def test_a_failure_one_second_short_of_a_day_still_pairs():
    late = call("p", START + IDLE_SECONDS - 1)
    result = pair_session(
        (), [failed("f", START, command="pytest --x tests"), late], START + IDLE_SECONDS - 1
    )
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)
