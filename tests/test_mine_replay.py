import pytest

from scars.match import Precision, RecordedCall, precision
from scars.mine.replay import CallIndex
from scars.model import Stage, ToolKind, Trigger, TriggerKind

SIG = "E: bad flag"


def shell(command: str, ok: bool, sig: str | None = None) -> RecordedCall:
    return RecordedCall(ToolKind.SHELL, command, (), ok, None if ok else sig, "/r")


CALLS = [
    shell("pytest --frobnicate tests", False, SIG),
    shell("pytest tests", True),
    shell("cd sub && pytest --frobnicate", False, SIG),
    shell("pytest --frobnicate x", False, "E: other"),
    shell("pytest --frobnicate x | tee log", True),
    shell("pytester --frobnicate", True),
    shell("echo 'unterminated", True),
    shell("git log -n bad", False, SIG),
    RecordedCall(ToolKind.OTHER, None, ("/r/src/app.py",), True, None, "/r"),
    RecordedCall(ToolKind.EDIT, None, ("src/lib.py",), True, None, "/r"),
    RecordedCall(ToolKind.EDIT, None, ("/elsewhere/src/x.py",), True, None, "/r"),
]


@pytest.mark.parametrize(
    "trigger",
    [
        Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.GATE),
        Trigger(TriggerKind.COMMAND, "pytest*", Stage.GATE),
        Trigger(TriggerKind.COMMAND, "git log -n *", Stage.GATE),
        Trigger(TriggerKind.COMMAND, "nothing-runs-this", Stage.GATE),
        Trigger(TriggerKind.PATH, "src/**", Stage.PLAN),
    ],
)
def test_index_counts_like_the_reference_precision(trigger):
    index = CallIndex(CALLS, lambda cwd: "/r")
    assert index.precision(trigger, SIG) == precision(trigger, CALLS, error_signature=SIG, repo_root="/r")


def test_index_counts_literal_values_and_memoizes():
    index = CallIndex(CALLS, lambda cwd: None)
    trigger = Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.GATE)
    assert index.precision(trigger, SIG) == Precision(2, 1, 4, 11)
    assert index.precision(trigger, SIG) is index.precision(trigger, SIG)
    path = Trigger(TriggerKind.PATH, "src/**", Stage.PLAN)
    assert index.precision(path, SIG) == Precision(0, 1, 1, 11)


def test_a_redirection_with_no_program_runs_no_null_command():
    calls = [shell("> /tmp/log", True), shell("cd /tmp > /dev/null && pytest", True)]
    index = CallIndex(calls, lambda cwd: None)
    assert index.precision(Trigger(TriggerKind.COMMAND, ":", Stage.GATE), SIG) == Precision(0, 0, 0, 2)
    assert index.precision(Trigger(TriggerKind.COMMAND, "pytest", Stage.GATE), SIG) == Precision(0, 1, 1, 2)


def test_index_rejects_error_triggers():
    with pytest.raises(ValueError):
        CallIndex(CALLS, lambda cwd: None).precision(Trigger(TriggerKind.ERROR, SIG, Stage.REPAIR), SIG)
