"""Codex hook stdin, as documented for Codex 0.162.1.

Events: `SessionStart` (source `compact` marks a compaction; `startup`, `resume`, `clear` and `fork` start
a session), `UserPromptSubmit`, `PreToolUse` on `Bash` and `apply_patch`, and `PostToolUse`. Codex reports
its `exec_command` tool as `Bash`, and file edits as `apply_patch` whose patch text is in
`tool_input.command`.

Codex has no failure event. `PostToolUse` fires after every call, and a call counts as failed only when
the response shows a non-zero exit code: an object with an integer `exit_code`, or a text response whose
header has a `Process exited with code N` or `Exit code: N` line. In Codex 0.162.1 the `Bash` response is
the output text without that header, so most failures carry no exit code. Those calls are ignored rather
than guessed at, because treating every response as a failure would fire repair lessons on successful
output.
"""

import re

from scars.adapters import Common, Ignored, MalformedInput, common, event, string, tool_input
from scars.model import Harness, HookEvent, HookEventKind, ToolKind

SOURCES = {
    "startup": HookEventKind.SESSION_START,
    "resume": HookEventKind.SESSION_START,
    "clear": HookEventKind.SESSION_START,
    "fork": HookEventKind.SESSION_START,
    "compact": HookEventKind.COMPACT,
}
_PATCH_HEADER = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)
_EXIT_LINE = re.compile(r"^(?:Process exited with code|Exit code:?) (-?\d+)\s*$")
_HEADER_LINES = 8


def parse(event_name: str, payload: dict[str, object]) -> HookEvent:
    base = common(event_name, payload)
    match event_name:
        case "SessionStart":
            source = string(payload, "source")
            if source not in SOURCES:
                raise MalformedInput(f"unknown SessionStart source {source!r}")
            return event(Harness.CODEX, SOURCES[source], base)
        case "UserPromptSubmit":
            return event(Harness.CODEX, HookEventKind.PROMPT, base, prompt=string(payload, "prompt"))
        case "PreToolUse":
            return _tool(HookEventKind.PRE_TOOL, base, payload)
        case "PostToolUse":
            code, output = exit_status(payload.get("tool_response"))
            if not code:
                raise Ignored("the call did not fail with a known exit code")
            return _tool(HookEventKind.TOOL_FAILED, base, payload, error_text=output)
        case _:
            raise MalformedInput(f"unknown Codex event {event_name!r}")


def exit_status(response: object) -> tuple[int | None, str]:
    """The exit code a tool response shows, or None, and its output text.

    A text response with a header keeps only the text after the header's `Output:` line, so the error
    signature is taken from the command's output and not from `Wall time` or the exit-code line.
    """
    if isinstance(response, dict):
        code = response.get("exit_code")
        output = next(
            (v for k in ("output", "stderr", "stdout") if isinstance(v := response.get(k), str)), ""
        )
        return (code if isinstance(code, int) and not isinstance(code, bool) else None), output
    if isinstance(response, str):
        lines = response.splitlines()
        code = None
        for i, line in enumerate(lines[:_HEADER_LINES]):
            if found := _EXIT_LINE.match(line.strip()):
                code = int(found.group(1))
            elif code is not None and line.strip() == "Output:":
                return code, "\n".join(lines[i + 1 :])
        return code, response
    return None, ""


def patch_paths(patch: str) -> tuple[str, ...]:
    """Every file an `apply_patch` patch adds, updates, deletes or moves to, in order."""
    return tuple(dict.fromkeys(_PATCH_HEADER.findall(patch)))


def _tool(
    kind: HookEventKind, base: Common, payload: dict[str, object], *, error_text: str | None = None
) -> HookEvent:
    name = string(payload, "tool_name")
    command = None
    paths: tuple[str, ...] = ()
    if name == "Bash":
        tool_kind = ToolKind.SHELL
        command = string(tool_input(payload), "command")
    elif name == "apply_patch":
        tool_kind = ToolKind.EDIT
        patch = string(tool_input(payload), "command")
        assert patch is not None
        paths = patch_paths(patch)
        if not paths:
            raise MalformedInput("apply_patch names no file")
    else:
        tool_kind = ToolKind.OTHER
    return event(
        Harness.CODEX,
        kind,
        base,
        tool_use_id=string(payload, "tool_use_id"),
        tool_kind=tool_kind,
        command=command,
        paths=paths,
        error_text=error_text,
    )
