import pytest

from scars.mine import build
from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import Pair, open_failure
from scars.model import Harness, RepoId
from scars.shell import SimpleCommand

START = 1_800_000_000.0
FAIL = "pytest: error: unrecognized arguments: --frobnicate"


def call(tool_use_id, command, status=CallStatus.OK, output=""):
    return make_call(
        harness=Harness.CLAUDE,
        session_id="s1",
        tool_use_id=tool_use_id,
        tool="Bash",
        raw_input={"command": command},
        command=command,
        paths=(),
        status=status,
        output=output,
        at=START,
        cwd="/tmp/app",
        model_id=None,
    )


def pair(failed_command, passing_command):
    failure = open_failure(call("f", failed_command, CallStatus.FAILED, FAIL))
    assert failure is not None
    return Pair(failure, call("p", passing_command))


def test_a_retry_of_the_same_words_is_no_lesson():
    with pytest.raises(build.Skipped) as caught:
        build.build(pair("pytest tests", "pytest   'tests'"), RepoId("r"), "own")
    assert caught.value.reason == "identical_retry"


def test_a_changed_argument_is_a_lesson():
    candidate = build.build(pair("pytest --frobnicate tests", "pytest tests"), RepoId("r"), "own")
    assert (candidate.text.avoid, candidate.text.instead) == (
        "pytest --frobnicate tests",
        "run `pytest tests` instead",
    )


def test_avoid_is_the_failed_command_as_run_not_the_arguments_the_fix_dropped():
    candidate = build.build(
        pair("sed  -n 1,240p   src/client.py", "sed -n 1,290p src/realtime/client.py"), RepoId("r"), "own"
    )
    assert (candidate.text.avoid, candidate.text.instead, candidate.failed_input) == (
        "sed -n 1,240p src/client.py",
        "run `sed -n 1,290p src/realtime/client.py` instead",
        "sed  -n 1,240p   src/client.py",
    )


@pytest.mark.parametrize(
    "secret_arg",
    [
        "--token=hunter2hunter2hunter2",
        "--password=hunter2hunter2hu",
        "--api-key=hunter2hunter2hunter2",
        "token=hunter2hunter2hunter2",
        "PASSWORD=hunter2hunter2hunter2",
        "secret=hunter2hunter2hunter2",
        "key=hunter2hunter2hunter2",
    ],
)
@pytest.mark.parametrize("side", ["failed", "passing"])
def test_an_argument_named_as_a_secret_is_never_mined(secret_arg, side):
    failed = f"npm publish --tag next {secret_arg}" if side == "failed" else "npm publish --tag next"
    passing = f"npm publish {secret_arg}" if side == "passing" else "npm publish"
    assert build.secret_rule(secret_arg) == "secret.keyed_argument"
    with pytest.raises(build.render.RejectedText) as caught:
        build.build(pair(failed, passing), RepoId("r"), "own")
    assert (caught.value.rule, caught.value.field) == ("secret.keyed_argument", f"{side}_input")


@pytest.mark.parametrize(
    "arg", ["--token=short", "--keyword=hunter2hunter2hunter2", "monkey=hunter2hunter2hunter2"]
)
def test_a_short_value_or_another_key_is_not_a_keyed_secret(arg):
    assert build.secret_rule(arg) is None


@pytest.mark.parametrize(
    ("failed", "spec"),
    [
        (SimpleCommand("pytest", ("--frobnicate", "tests"), None), "pytest ** --frobnicate"),
        (SimpleCommand("pytest", ("--", "--frobnicate"), None), None),
        (SimpleCommand("pytest", ("$X", "--frobnicate"), None, frozenset({0})), None),
    ],
)
def test_pre_trigger_spec_stops_at_double_dash_and_at_an_expanded_argument(failed, spec):
    assert build.pre_trigger_spec(failed, frozenset({"--frobnicate"})) == spec
