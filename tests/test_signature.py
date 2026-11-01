import pytest

from scars.match import ErrorPattern, parse_spec
from scars.model import ErrorSignature, TriggerKind
from scars.signature import signature, tool_signature


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


def test_signature_strips_whitespace_at_the_cut_for_an_error_trigger():
    output = (
        "Error: the requested operation could not complete because required dependency "
        "is unavailable in the current environment before retry"
    )
    expected = (
        "Error: the requested operation could not complete because required dependency "
        "is unavailable in the current environment"
    )

    sig = signature(output)

    assert sig == expected
    assert parse_spec(TriggerKind.ERROR, sig) == ErrorPattern(ErrorSignature(expected))


def test_tool_signature_at_the_cut_is_accepted_as_an_error_trigger():
    output = (
        "Error: the requested operation could not complete because required dependency "
        "is unavailable in the current environment before retry"
    )
    expected = (
        "[tool X] Error: the requested operation could not complete because required dependency "
        "is unavailable in the current environment"
    )
    sig = signature(output)
    assert sig is not None

    qualified = tool_signature("X", sig)

    assert qualified == expected
    assert parse_spec(TriggerKind.ERROR, qualified) == ErrorPattern(ErrorSignature(expected))


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


@pytest.mark.parametrize(
    "output",
    [
        "[tool Read] invalid mode",
        "[tool Read] unsupported mode",
        "Exit code 1\n  [tool Read] invalid mode",
        "\x1b[31m[tool Read] invalid mode\x1b[0m",
        "[tool\tRead] invalid mode",
    ],
)
def test_raw_signature_cannot_use_the_tool_namespace(output):
    assert signature(output) is None
    assert signature("invalid mode") == "invalid mode"
    assert tool_signature("Read", ErrorSignature("invalid mode")) == "[tool Read] invalid mode"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("# Contributing...\ncat: x: No such file or directory", "cat: x: No such file or directory"),
        (
            "==> forum\nsed: src/config.py: No such file or directory",
            "sed: src<path>: No such file or directory",
        ),
        (
            "import asyncio\nModuleNotFoundError: No module named 'fitz'",
            "ModuleNotFoundError: No module named 'fitz'",
        ),
        ('# Useful Commands\nUnknown JSON field: "message"', 'Unknown JSON field: "message"'),
        (
            '# Investigation\nInvalid search query "repo:example/project".',
            'Invalid search query "repo:example<path>".',
        ),
        (".github/workflows\ngh: Not Found (HTTP 404)", "gh: Not Found (HTTP <n>)"),
        ("# Refactoring\nfatal: Needed a single revision", "fatal: Needed a single revision"),
        (
            "# Individual Contributor License Agreement\n(eval):7: no matches found: --include=*.swift",
            "(eval):<n>: no matches found: --include=*.swift",
        ),
        (
            "# Opening a PR\n(eval):3: permission denied: /tmp/project/check.sh",
            "(eval):<n>: permission denied: <path>",
        ),
        ("# Contributing...\n\x1b[31mERROR: build failed\x1b[0m", "ERROR: build failed"),
        ("# Contributing...\nerror:\ncat: x: No such file or directory", "cat: x: No such file or directory"),
    ],
)
def test_signature_prefers_an_error_line_over_stdout(output, expected):
    assert signature(output) == expected
