import pytest

from scars.mine.outcomes import HitRow, LessonFacts, decide
from scars.model import DelayedOutcome, LessonId, Stage, Trigger, TriggerKind
from scars.store import MinedCall

T0 = 1_800_000_000.0
SIG = "E: broken"
PRE = Trigger(TriggerKind.COMMAND, "pytest ** --frobnicate", Stage.PLAN)
FACTS = LessonFacts(SIG, (PRE,))
BAD = "pytest --frobnicate tests"
GOOD = "pytest tests"


def shell(i: int, status: str = "ok", command: str = GOOD, sig: str | None = None) -> MinedCall:
    return MinedCall(f"c{i}", "claude", "s", T0 + i, "Bash", "shell", command, (), status, sig, "/r", None)


def read(i: int) -> MinedCall:
    return MinedCall(f"c{i}", "claude", "s", T0 + i, "Read", "other", None, ("/r/a",), "ok", None, "/r", None)


def failing(i: int, sig: str = SIG, command: str = BAD) -> MinedCall:
    return shell(i, "failed", command, sig)


REPAIR = HitRow(1, T0, "s", "c0", Stage.REPAIR, LessonId("L-1"))
PLAN = HitRow(2, T0 + 0.5, "s", None, Stage.PLAN, LessonId("L-1"))


@pytest.mark.parametrize(
    ("calls", "ended", "expected"),
    [
        # The lesson's error recurs within the 5 calls after the failure that fired the hit.
        ([failing(0), read(1), read(2), failing(3)], False, DelayedOutcome.IGNORED_THEN_FAILED),
        # A later failure wins over an earlier success of the same tool kind.
        ([failing(0), shell(1), read(2), read(3), failing(4)], False, DelayedOutcome.IGNORED_THEN_FAILED),
        # The next shell call succeeded, and the window has closed.
        ([failing(0), shell(1), read(2), read(3), read(4), read(5)], False, DelayedOutcome.HEEDED),
        # The next shell call failed differently and no longer matches the pre-trigger.
        (
            [failing(0), failing(1, "other", GOOD), read(2), read(3), read(4), read(5)],
            False,
            DelayedOutcome.HEEDED,
        ),
        # The next shell call still matches the pre-trigger and failed differently.
        (
            [failing(0), failing(1, "other"), read(2), read(3), read(4), read(5)],
            False,
            DelayedOutcome.UNKNOWN,
        ),
        # Only 1 call after the failure: undecided while the session is live.
        ([failing(0), shell(1)], False, None),
        ([failing(0), shell(1)], True, DelayedOutcome.HEEDED),
        # The recurrence is the 5th call after: still inside the window.
        (
            [failing(0), read(1), read(2), read(3), read(4), failing(5)],
            False,
            DelayedOutcome.IGNORED_THEN_FAILED,
        ),
        # The recurrence is the 6th call after: outside the window, and the next shell call is that failure.
        (
            [failing(0), read(1), read(2), read(3), read(4), read(5), failing(6)],
            False,
            DelayedOutcome.UNKNOWN,
        ),
        # The window closed with no shell call yet: wait for one, or give up when the session ends.
        ([failing(0), read(1), read(2), read(3), read(4), read(5)], False, None),
        ([failing(0), read(1), read(2), read(3), read(4), read(5)], True, DelayedOutcome.UNKNOWN),
        # The failure that fired the hit was never mined.
        ([shell(1)], False, None),
        ([shell(1)], True, DelayedOutcome.UNKNOWN),
    ],
)
def test_repair_rules(calls, ended, expected):
    assert decide(REPAIR, FACTS, calls, ended) is expected


def test_repair_hit_of_a_lesson_without_pre_trigger_is_heeded_only_by_success():
    facts = LessonFacts(SIG, ())
    closed = [read(2), read(3), read(4), read(5)]
    assert (
        decide(REPAIR, facts, [failing(0), failing(1, "other", GOOD), *closed], False)
        is DelayedOutcome.UNKNOWN
    )
    assert decide(REPAIR, facts, [failing(0), shell(1), *closed], False) is DelayedOutcome.HEEDED


@pytest.mark.parametrize(
    ("calls", "ended", "expected"),
    [
        # The matching call itself failed with the lesson's error.
        ([read(1), failing(2)], False, DelayedOutcome.IGNORED_THEN_FAILED),
        # It succeeded, then the error came within 5 calls: the failure takes precedence.
        ([shell(1, command=BAD), read(2), failing(5)], False, DelayedOutcome.IGNORED_THEN_FAILED),
        # It succeeded and 5 clean calls followed.
        ([shell(1, command=BAD), *map(read, range(2, 7))], False, DelayedOutcome.FIRED_UNNEEDED),
        # It succeeded but the window is still open.
        ([shell(1, command=BAD), read(2)], False, None),
        ([shell(1, command=BAD), read(2)], True, DelayedOutcome.FIRED_UNNEEDED),
        # It failed with another error and the window closed: no rule holds.
        ([failing(1, "other"), *map(read, range(2, 7))], False, DelayedOutcome.UNKNOWN),
        # No call matched the pre-trigger.
        ([shell(1), read(2)], False, None),
        ([shell(1), read(2)], True, DelayedOutcome.NO_ATTEMPT),
    ],
)
def test_plan_rules(calls, ended, expected):
    assert decide(PLAN, FACTS, calls, ended) is expected


def test_plan_hit_ignores_matching_calls_before_the_hit():
    assert decide(PLAN, FACTS, [failing(0), shell(1)], True) is DelayedOutcome.NO_ATTEMPT
