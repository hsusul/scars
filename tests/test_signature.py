import pytest

from scars.signature import signature


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Exit code 1\n(eval):cd:1: no such file or directory: apps/web",
            "(eval):cd:<n>: no such file or directory: apps<path>",
        ),
        (
            "Traceback (most recent call last):\n  File \"<stdin>\", line 3, in <module>\nKeyError: 'name'",
            "KeyError: 'name'",
        ),
        (
            "No simulator with UDID 1A2B3C4D-1111-2222-3333-444455556666. Pass a real device",
            "No simulator with UDID <uuid>. Pass a real device",
        ),
        (
            'Working directory "/Volumes/990 Pro/OpenSource/x-wt-1" was deleted; shell cwd recovered',
            'Working directory "<vol><path>" was deleted; shell cwd recovered',
        ),
    ],
)
def test_signature_normalizes_variable_parts(output, expected):
    assert signature(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "The user doesn't want to proceed with this tool use.",
        "Command timed out after 2m 0s",
        "Exit code 1\n---\n42",
    ],
)
def test_signature_is_none_for_output_no_lesson_can_address(output):
    assert signature(output) is None


@pytest.mark.parametrize(
    "output",
    [
        "[scars L-1a2b3c4d note | repo app | seen 2x | unreviewed]\nError: boom",
        "Error: boom, see [SCARS L-1a2b warn] for the fix",
    ],
)
def test_output_carrying_a_scars_header_is_never_a_signature(output):
    assert signature(output) is None
