import pytest

from scars.cli import PHASE_1_COMMANDS
from scars.model import LIFECYCLE, PRIVILEGED_COMMANDS, EventType, LessonText, Status


def test_lesson_text_accepts_fields_at_the_cap():
    text = LessonText(avoid="a" * 240, instead="b", reason="c")
    assert len(text.avoid) == 240


@pytest.mark.parametrize(("avoid", "instead"), [("a" * 241, "b"), ("a", "")])
def test_lesson_text_rejects_fields_outside_the_cap(avoid, instead):
    with pytest.raises(ValueError):
        LessonText(avoid=avoid, instead=instead, reason="c")


def test_every_event_type_has_a_lifecycle_transition():
    assert set(LIFECYCLE) == set(EventType)


def test_terminal_statuses_accept_no_event_except_proposing_a_new_lesson():
    terminal = {Status.RETIRED, Status.SUPERSEDED}
    reachable_from_terminal = [e for e, t in LIFECYCLE.items() if t.allowed_from & terminal]
    assert reachable_from_terminal == []


def test_phase_1_privileged_commands_exist_in_the_cli():
    phase_2_only = {"supersede", "verify", "quarantine"}
    assert (PRIVILEGED_COMMANDS - phase_2_only) <= {c.name for c in PHASE_1_COMMANDS}
