"""Per-harness parsing of hook stdin and rendering of hook output.

Each harness module exposes `parse(event_name, payload) -> HookEvent`. Parsing happens once, here, at the
boundary: a payload that is not the documented shape raises `MalformedInput`, and an event that scars
deliberately does not act on raises `Ignored`. The hook turns either into no output.

Output is limited to three shapes, shared by both harnesses: context through
`hookSpecificOutput.additionalContext`, a deny through `permissionDecision: "deny"` with its reason, and
a human notice through the top-level `systemMessage`. No other field is ever emitted, because `allow`,
`ask`, `updatedInput`, `continue` or `decision` would let scars skip or rewrite a permission check that a
standalone guard or the user's settings own.
"""

from dataclasses import dataclass

from scars.model import Harness, HookEvent, HookEventKind, SessionId, ToolKind

FIELD_CAP_BYTES = 4096
TRUNCATION_MARK = "\x00"
"""Appended to a cut command. The shell parser rejects every control character, NUL included, so a cut
command always parses as unsupported instead of matching as the shorter command it was cut to, whatever
its length after the cut."""


class AdapterError(Exception):
    """The hook prints nothing for this stdin."""


class MalformedInput(AdapterError):
    """Stdin is not JSON, or not the shape the harness documents for this event."""


class Ignored(AdapterError):
    """A well-formed event that scars does not act on (another harness, or a call that succeeded)."""


def cap(text: str) -> str:
    """`text` cut to at most 4 KB of UTF-8, never splitting a character."""
    raw = text.encode()
    if len(raw) <= FIELD_CAP_BYTES:
        return text
    return raw[:FIELD_CAP_BYTES].decode(errors="ignore")


def cap_command(command: str) -> str:
    """`command` itself when it fits in 4 KB, else its cut followed by `TRUNCATION_MARK`."""
    capped = cap(command)
    return capped if capped == command else capped + TRUNCATION_MARK


def full_command(payload: dict[str, object]) -> str | None:
    """The uncapped `tool_input.command` of a tool event, which both harnesses use for shell commands.

    The agent deny rule reads this instead of `HookEvent.command`, so text past the 4 KB cut still counts.
    """
    args = payload.get("tool_input")
    command = args.get("command") if isinstance(args, dict) else None
    return command if isinstance(command, str) else None


def string(payload: dict[str, object], key: str, *, required: bool = True) -> str | None:
    """A string field. A required one must be present and non-empty; an optional one may be absent."""
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise MalformedInput(f"{key} must be a non-empty string")
    return value


def tool_input(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        raise MalformedInput("tool_input must be an object")
    return value


@dataclass(frozen=True)
class Common:
    session_id: SessionId
    cwd: str
    model_id: str | None


def common(event_name: str, payload: dict[str, object]) -> Common:
    """Check the fields every event carries. A `cursor_version` field means Cursor is running a Claude
    Code hook, which would deliver the same lesson twice, so the event is ignored."""
    if "cursor_version" in payload:
        raise Ignored("Cursor session")
    if payload.get("hook_event_name") != event_name:
        raise MalformedInput(f"hook_event_name is not {event_name}")
    session_id = string(payload, "session_id")
    cwd = string(payload, "cwd")
    assert session_id is not None and cwd is not None
    return Common(SessionId(session_id), cwd, string(payload, "model", required=False) or None)


def event(
    harness: Harness,
    kind: HookEventKind,
    base: Common,
    *,
    tool_use_id: str | None = None,
    tool_kind: ToolKind | None = None,
    command: str | None = None,
    paths: tuple[str, ...] = (),
    error_text: str | None = None,
    prompt: str | None = None,
    tool_name: str | None = None,
) -> HookEvent:
    """Build the normalized event with every free-text field capped at 4 KB.

    `harness_version` is empty: Claude Code does not put its version in hook stdin or in the hook's
    environment (Claude Code 2.1.299 exports `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`,
    `CLAUDE_PLUGIN_DATA`, `CLAUDE_EFFORT` and `CLAUDE_CODE_REMOTE`), and running `claude --version`
    would cost more than the whole hook budget. The hit row stores NULL until a harness exports one.
    """
    return HookEvent(
        harness=harness,
        harness_version="",
        event=kind,
        session_id=base.session_id,
        tool_use_id=tool_use_id,
        cwd=base.cwd,
        tool_kind=tool_kind,
        command=cap_command(command) if command is not None else None,
        paths=paths,
        error_text=cap(error_text) if error_text is not None else None,
        prompt=cap(prompt) if prompt is not None else None,
        model_id=base.model_id,
        tool_name=tool_name,
    )


def context_output(
    hook_event_name: str, text: str | None, notice: str | None = None
) -> dict[str, object] | None:
    """Context for the model, a notice for the human, both, or None when there is nothing to say."""
    out: dict[str, object] = {}
    if notice:
        out["systemMessage"] = notice
    if text:
        out["hookSpecificOutput"] = {"hookEventName": hook_event_name, "additionalContext": text}
    return out or None


def deny_output(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
