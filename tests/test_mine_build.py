import pytest

from scars.mine import build
from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import Pair, open_failure
from scars.model import Harness, RepoId
from scars.shell import SimpleCommand

T0 = 1_800_000_000.0
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
        at=T0,
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
        "pytest --frobnicate",
        "run `pytest tests` instead",
    )


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
