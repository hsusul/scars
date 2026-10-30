"""Claude Code hook stdin, as documented for Claude Code 2.1.299.

Events: `SessionStart` (sources `startup`, `resume`, `clear` and `fork` start a session; `compact` marks a
compaction), `UserPromptSubmit`, `PostCompact`, `PreToolUse` and `PostToolUseFailure`. Claude Code's
`PostToolUse` fires only on success, so repair uses `PostToolUseFailure` alone.

`PostCompact` is accepted and ignored. Claude Code gives it no decision control and discards its
context, while `SessionStart` with source `compact` fires after the same compaction and can deliver. That
event alone moves the context epoch, so one compaction counts once and the lessons re-injected after it
are recorded in the new epoch whatever order the two events arrive in.
"""

from scars.adapters import Ignored, MalformedInput, common, event, string, tool_input
from scars.model import Harness, HookEvent, HookEventKind, ToolKind

SOURCES = {
    "startup": HookEventKind.SESSION_START,
    "resume": HookEventKind.SESSION_START,
    "clear": HookEventKind.SESSION_START,
    "fork": HookEventKind.SESSION_START,
    "compact": HookEventKind.COMPACT,
}
SHELL_TOOLS = frozenset({"Bash"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})


def parse(event_name: str, payload: dict[str, object]) -> HookEvent:
    base = common(event_name, payload)
    match event_name:
        case "SessionStart":
            source = string(payload, "source")
            if source not in SOURCES:
                raise MalformedInput(f"unknown SessionStart source {source!r}")
            return event(Harness.CLAUDE, SOURCES[source], base)
        case "UserPromptSubmit":
            return event(Harness.CLAUDE, HookEventKind.PROMPT, base, prompt=string(payload, "prompt"))
        case "PostCompact":
            raise Ignored("PostCompact cannot deliver context; SessionStart with source compact does")
        case "PreToolUse":
            return _tool(HookEventKind.PRE_TOOL, base, payload)
        case "PostToolUseFailure":
            return _tool(HookEventKind.TOOL_FAILED, base, payload, error_text=string(payload, "error"))
        case _:
            raise MalformedInput(f"unknown Claude Code event {event_name!r}")


def _tool(
    kind: HookEventKind, base, payload: dict[str, object], *, error_text: str | None = None
) -> HookEvent:
    name = string(payload, "tool_name")
    args = tool_input(payload)
    command = None
    paths: tuple[str, ...] = ()
    if name in SHELL_TOOLS:
        tool_kind = ToolKind.SHELL
        command = string(args, "command")
    elif name in EDIT_TOOLS:
        tool_kind = ToolKind.EDIT
        file_path = string(args, "file_path")
        assert file_path is not None
        paths = (file_path,)
    else:
        tool_kind = ToolKind.OTHER
    return event(
        Harness.CLAUDE,
        kind,
        base,
        tool_use_id=string(payload, "tool_use_id"),
        tool_kind=tool_kind,
        command=command,
        paths=paths,
        error_text=error_text,
    )
