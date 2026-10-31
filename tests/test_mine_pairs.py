from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import IDLE_SECONDS, pair_session
from scars.model import Harness

T0 = 1_800_000_000.0
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
    result = pair_session((), [failed("f", T0, command="pytest --x"), call("p", T0 + 1)], T0 + 2)
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)


def test_a_failed_edit_call_never_opens():
    edit = failed("f", T0, tool="Edit")
    result = pair_session((), [edit, call("r", T0 + 1, tool="Read")], T0 + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failed_shell_call_the_parser_rejects_never_opens():
    result = pair_session(
        (), [failed("f", T0, command="pytest $("), call("p", T0 + 1, command="echo $(")], T0 + 2
    )
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_with_no_lessonable_signature_never_opens():
    timeout = call(
        "f", T0, command="pytest --x", status=CallStatus.FAILED, output="Command timed out after 2m 0s"
    )
    result = pair_session((), [timeout, call("p", T0 + 1)], T0 + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_idle_for_a_day_is_dropped_by_the_next_call():
    late = call("p", T0 + IDLE_SECONDS)
    result = pair_session((), [failed("f", T0, command="pytest --x"), late], T0 + IDLE_SECONDS)
    assert outcome(result) == ([], [], 1, 0, 1)


def test_a_failure_one_second_short_of_a_day_still_pairs():
    late = call("p", T0 + IDLE_SECONDS - 1)
    result = pair_session((), [failed("f", T0, command="pytest --x"), late], T0 + IDLE_SECONDS - 1)
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)
