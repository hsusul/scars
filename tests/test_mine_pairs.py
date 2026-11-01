import pytest

from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import IDLE_SECONDS, Target, pair_session, same_target, target
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


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("cat CONTRIBUTING.md; rg missing .github", "# Contributing..."),
        ("cat AGENTS.md; rg missing src", "# Useful Commands"),
        ("sed -n '1,5p' config.py | rg missing", "import asyncio"),
        ("tail -n 100 audit.md && git status --short", "# Contribution chain run 2"),
        ("cat README.md || rg missing src", "# Individual Contributor License Agreement"),
    ],
)
def test_compound_stdout_without_an_error_never_opens_a_failure(command, output):
    result = pair_session(
        (), [call("f", START, command=command, status=CallStatus.FAILED, output=output)], START + 1
    )
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
            Target("git", ("push",), ("origin", "main"), {"-C": ("app",)}),
        ),
        (
            "git worktree add -b B upstream/master /p",
            Target("git", ("worktree", "add"), ("B", "upstream/master", "/p"), {}),
        ),
        ("gh pr diff 1801 --repo o/r", Target("gh", ("pr", "diff"), ("1801",), {"--repo": ("o/r",)})),
        ("kubectl --namespace=a get pods", Target("kubectl", ("get",), ("pods",), {"--namespace": ("a",)})),
        ("tail -n 5 build.log", Target("tail", (), ("build.log",), {})),
        ("rm -f a", Target("rm", (), ("a",), {})),
        (
            "kubectl --kubeconfig cfg get pods",
            Target("kubectl", ("get",), ("pods",), {"--kubeconfig": ("cfg",)}),
        ),
        ("npm run build", Target("npm", ("run",), ("build",), {})),
        ("xcrun simctl boot 1111-AAAA", Target("xcrun", ("simctl", "boot"), ("1111-AAAA",), {})),
        ("xcodebuild -scheme App build", Target("xcodebuild", (), ("build",), {"-scheme": ("App",)})),
        ("make -C app test", Target("make", (), ("test",), {"--directory": ("app",)})),
        ("cat --number -- -notes.md", Target("cat", (), ("-notes.md",), {})),
        ("gh api repos/o/r?ref=x", Target("gh", ("api", "repos/o/r?ref=x"), (), {})),
        ("cat $NOTE", Target("cat", (), ("$NOTE",), {})),
    ],
)
def test_the_target_is_the_subcommand_and_positional_operands(command, expected):
    assert target(command) == expected


def test_commands_of_two_programs_never_share_a_target():
    assert (same_target("cat a", "less a"), same_target("cat a", "cat --number a")) == (False, True)


def test_a_failure_one_second_short_of_a_day_still_pairs():
    late = call("p", START + IDLE_SECONDS - 1)
    result = pair_session(
        (), [failed("f", START, command="pytest --x tests"), late], START + IDLE_SECONDS - 1
    )
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)


NEW_SELECTORS = [
    ("pnpm", "-C", "test"),
    ("pnpm", "--dir", "test"),
    ("pnpm", "--filter", "test"),
    ("pnpm", "-F", "test"),
    ("yarn", "--cwd", "test"),
    ("uv", "--directory", "sync"),
    ("uv", "--project", "sync"),
    ("xcodebuild", "-project", "build"),
    ("xcodebuild", "-workspace", "build"),
    ("xcodebuild", "-scheme", "build"),
    ("kubectl", "--kubeconfig", "get pods"),
    ("kubectl", "-l", "get pods"),
    ("kubectl", "--selector", "get pods"),
]


@pytest.mark.parametrize(("program", "option", "action"), NEW_SELECTORS)
def test_new_selectors_skip_a_different_target_and_wait_for_the_fix(program, option, action):
    result = pair_session(
        (),
        [
            failed("f", START, command=f"{program} {option}=app {action}"),
            call("other", START + 1, command=f"{program} {option}=other {action}"),
            call("fix", START + 2, command=f"{program} {option} app {action}"),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


@pytest.mark.parametrize(("program", "option", "action"), NEW_SELECTORS)
def test_adding_or_removing_a_target_selector_still_pairs(program, option, action):
    results = [
        pair_session(
            (),
            [failed("f", START, command=a), call("p", START + 1, command=b)],
            START + 2,
        )
        for a, b in [
            (f"{program} {action}", f"{program} {option} app {action}"),
            (f"{program} {option} app {action}", f"{program} {action}"),
        ]
    ]
    assert [(outcome(result), result.operand_mismatch) for result in results] == [
        (([("f", "p")], [], 1, 0, 0), 0),
        (([("f", "p")], [], 1, 0, 0), 0),
    ]


@pytest.mark.parametrize(
    ("program", "short", "long", "action"),
    [
        ("git", "-C", "-C", "status"),
        ("gh", "-R", "--repo", "pr list"),
        ("npm", "-w", "--workspace", "test"),
        ("make", "-C", "--directory", "test"),
        ("make", "-f", "--file", "test"),
        ("cargo", "-p", "--package", "build"),
        ("kubectl", "-n", "--namespace", "get pods"),
        ("kubectl", "-l", "--selector", "get pods"),
        ("docker", "-H", "--host", "ps"),
        ("docker", "-c", "--context", "ps"),
        ("pnpm", "-C", "--dir", "test"),
        ("pnpm", "-F", "--filter", "test"),
    ],
)
@pytest.mark.parametrize(("value", "other"), [("app", "other"), ("app=one", "other=two")])
def test_attached_short_selectors_compare_values_and_match_separate_aliases(
    program, short, long, action, value, other
):
    result = pair_session(
        (),
        [
            failed("f", START, command=f"{program} {short}{value} {action}"),
            call("other", START + 1, command=f"{program} {short}{other} {action}"),
            call("fix", START + 2, command=f"{program} {long} {value} {action}"),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)
