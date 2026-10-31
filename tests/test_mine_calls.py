import pytest

from scars.mine.calls import UNRESOLVED_HORIZON, Chunk, Invocation, Line, read_from, settle, shell_program


@pytest.mark.parametrize(
    ("command", "program"),
    [
        ("cd /tmp > /dev/null && pytest", "pytest"),
        ("> /readonly/f", None),
        ("X=1 > f; make", "make"),
    ],
)
def test_shell_program_skips_redirections_that_run_no_program(command, program):
    assert shell_program(command) == program


START = 1_800_000_000.0


def test_an_unresolved_call_holds_the_offset_until_the_horizon():
    pending = [Invocation(offset=7, at=START)]
    assert settle(pending, newest=START, now=START + UNRESOLVED_HORIZON - 1) == ([], 7)


def test_an_unresolved_call_past_the_horizon_is_abandoned():
    pending = [Invocation(offset=7, at=START)]
    assert settle(pending, newest=START, now=START + UNRESOLVED_HORIZON) == ([], None)


def test_the_horizon_also_counts_from_the_newest_record():
    pending = [Invocation(offset=7, at=START)]
    assert settle(pending, newest=START + UNRESOLVED_HORIZON, now=START) == ([], None)


def test_malformed_and_non_object_lines_are_counted_and_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'not json\n{"a": 1}\n[1]\n\n{"b": 2')
    assert read_from(str(path), 0) == Chunk(lines=(Line(offset=9, record={"a": 1}),), end=23, malformed=2)
