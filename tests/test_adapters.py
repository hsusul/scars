import dataclasses
import json
from pathlib import Path

import pytest

from scars import adapters
from scars.adapters import Ignored, MalformedInput, claude, codex
from scars.model import Harness, HookEventKind, ToolKind
from scars.shell import Unsupported, parse

FIXTURES = Path(__file__).parent / "fixtures"
MODULES = {"claude": claude, "codex": codex}
ERRORS = {"MalformedInput": MalformedInput, "Ignored": Ignored}


def fixture_cases():
    for path in sorted(FIXTURES.glob("*/*/*.json")):
        harness, version, event_name = path.parts[-3], path.parts[-2], path.stem
        for case in json.loads(path.read_text())["cases"]:
            yield pytest.param(
                harness, event_name, case, id=f"{harness}/{version}/{event_name}/{case['name']}"
            )


def as_json(event):
    data = dataclasses.asdict(event)
    data["paths"] = list(data["paths"])
    return data


@pytest.mark.parametrize(("harness", "event_name", "case"), list(fixture_cases()))
def test_golden_fixture(harness, event_name, case):
    parse = MODULES[harness].parse
    if "error" in case:
        with pytest.raises(ERRORS[case["error"]]):
            parse(event_name, case["stdin"])
    else:
        assert as_json(parse(event_name, case["stdin"])) == case["event"]


def test_every_mapped_event_has_a_fixture():
    names = {(p.parts[-3], p.stem) for p in FIXTURES.glob("*/*/*.json")}
    assert names == {
        ("claude", "SessionStart"),
        ("claude", "UserPromptSubmit"),
        ("claude", "PostCompact"),
        ("claude", "PreToolUse"),
        ("claude", "PostToolUseFailure"),
        ("codex", "SessionStart"),
        ("codex", "UserPromptSubmit"),
        ("codex", "PreToolUse"),
        ("codex", "PostToolUse"),
    }


def bash(command, **extra):
    return {
        "session_id": "s",
        "cwd": "/r",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_use_id": "t",
        "tool_input": {"command": command},
        **extra,
    }


@pytest.mark.parametrize("event_name", ["Stop", "PostToolUse", "Notification"])
def test_claude_rejects_events_outside_the_event_map(event_name):
    with pytest.raises(MalformedInput):
        claude.parse(event_name, {**bash("ls"), "hook_event_name": event_name})


def test_codex_rejects_events_outside_the_event_map():
    with pytest.raises(MalformedInput):
        codex.parse("PostToolUseFailure", {**bash("ls"), "hook_event_name": "PostToolUseFailure"})


def test_cursor_version_is_skipped_on_every_event():
    with pytest.raises(Ignored):
        claude.parse("PreToolUse", bash("ls", cursor_version="1.7"))


def test_a_command_over_4_kb_is_cut_and_still_parses_as_unsupported():
    event = claude.parse("PreToolUse", bash("echo " + "a" * 5000))
    assert event.command == "echo " + "a" * 4091 + "\x00"
    with pytest.raises(Unsupported):
        parse(event.command)


def test_a_cut_that_drops_a_multibyte_character_still_parses_as_unsupported():
    command = "xcodebuild -scheme " + "a" * 4074 + "\N{GRINNING FACE} -showsdks"
    event = claude.parse("PreToolUse", bash(command))
    assert event.command == "xcodebuild -scheme " + "a" * 4074 + "\x00"
    with pytest.raises(Unsupported):
        parse(event.command)


@pytest.mark.parametrize("length", range(4090, 4100))
def test_every_cut_command_is_unsupported(length):
    for filler in ("a", "\N{GRINNING FACE}", "\N{LATIN SMALL LETTER E WITH ACUTE}"):
        command = ("ls " + filler * length)[:length]
        capped = claude.parse("PreToolUse", bash(command)).command
        if capped != command:
            with pytest.raises(Unsupported):
                parse(capped)


def test_a_command_of_exactly_4_kb_is_kept_whole():
    command = "echo " + "a" * 4091
    assert claude.parse("PreToolUse", bash(command)).command == command


def test_error_text_and_prompt_are_capped_at_4_kb_without_splitting_characters():
    failure = {**bash("ls"), "hook_event_name": "PostToolUseFailure", "error": "é" * 3000}
    assert claude.parse("PostToolUseFailure", failure).error_text == "é" * 2048
    prompt = {"session_id": "s", "cwd": "/r", "hook_event_name": "UserPromptSubmit", "prompt": "x" * 9000}
    assert codex.parse("UserPromptSubmit", prompt).prompt == "x" * 4096


def test_codex_post_tool_use_reads_a_nonzero_exit_header_and_ignores_zero():
    payload = {**bash("ls"), "hook_event_name": "PostToolUse", "tool_response": "Exit code: 0\nOutput:\nok"}
    with pytest.raises(Ignored):
        codex.parse("PostToolUse", payload)
    payload["tool_response"] = "Exit code: 3\nOutput:\nboom"
    event = codex.parse("PostToolUse", payload)
    assert (event.event, event.tool_kind, event.error_text) == (
        HookEventKind.TOOL_FAILED,
        ToolKind.SHELL,
        "boom",
    )


def test_codex_apply_patch_without_file_headers_has_no_paths_on_both_events():
    patch = {"command": "*** Begin Patch\n*** End Patch\n"}
    pre = {**bash("x"), "tool_name": "apply_patch", "tool_input": patch}
    assert codex.parse("PreToolUse", pre).paths == ()
    post = {**pre, "hook_event_name": "PostToolUse", "tool_response": "Exit code: 1\nOutput:\nbad patch"}
    event = codex.parse("PostToolUse", post)
    assert (event.tool_kind, event.paths, event.error_text) == (ToolKind.EDIT, (), "bad patch")


def test_codex_header_without_an_output_line_keeps_the_whole_text():
    assert codex.exit_status("Process exited with code 2\nboom") == (2, "Process exited with code 2\nboom")


def test_codex_exit_code_must_be_an_integer_not_a_boolean():
    assert codex.exit_status({"exit_code": True, "output": "x"}) == (None, "x")
    assert codex.exit_status(["Exit code: 1"]) == (None, "")


def test_codex_exit_header_only_counts_near_the_top():
    late = "\n".join(["line"] * 20 + ["Process exited with code 1"])
    assert codex.exit_status(late) == (None, late)


def test_context_output_shape():
    assert adapters.context_output("PostToolUseFailure", "lesson") == {
        "hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": "lesson"}
    }
    assert adapters.context_output("SessionStart", "", "2 scars lessons waiting: run scars inbox") == {
        "systemMessage": "2 scars lessons waiting: run scars inbox"
    }
    assert adapters.context_output("SessionStart", "", None) is None


def test_deny_output_shape_is_deny_and_reason_only():
    assert adapters.deny_output("no") == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no",
        }
    }


def test_harness_tags_follow_the_adapter():
    assert claude.parse("PreToolUse", bash("ls")).harness is Harness.CLAUDE
    assert codex.parse("PreToolUse", bash("ls")).harness is Harness.CODEX
