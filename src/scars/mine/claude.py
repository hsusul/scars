"""Claude Code transcripts: `~/.claude/projects/<project>/<session>.jsonl`.

Assistant records carry `tool_use` blocks (`id`, `name`, `input`) in `message.content`, with the model in
`message.model`. User records carry the matching `tool_result` blocks (`tool_use_id`, `is_error`,
`content`), with `timestamp` and `cwd` on the record. A call is complete when its result is read; its
time is the result's time, and its directory is the cwd of the record that invoked it: the result
record's cwd can already show a later `cd`. Calls are emitted in the order they were invoked, not the
order their results arrived, and an edit is emitted at its invocation even before its result (see
`calls.settle`).
The session id is the file name, as in hook stdin.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from scars.mine.calls import (
    EDIT_TOOLS,
    SHELL_TOOL,
    Call,
    CallStatus,
    Context,
    Invocation,
    Line,
    Parsed,
    make_call,
    settle,
    timestamp,
)
from scars.model import Harness


@dataclass(frozen=True)
class _Use:
    offset: int
    name: str
    input: object
    model: str | None
    cwd: str
    at: float | None


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _paths(args: object) -> tuple[str, ...]:
    if not isinstance(args, dict):
        return ()
    return tuple(v for k in ("file_path", "notebook_path") if isinstance(v := args.get(k), str))


def _call(
    session_id: str, tool_use_id: str, use: _Use, status: CallStatus, output: str, at: float, cwd: str
) -> Call:
    args = use.input
    command = args.get("command") if isinstance(args, dict) else None
    return make_call(
        harness=Harness.CLAUDE,
        session_id=session_id,
        tool_use_id=tool_use_id,
        tool=use.name,
        raw_input=args,
        command=command if use.name == SHELL_TOOL and isinstance(command, str) else None,
        paths=_paths(args),
        status=status,
        output=output,
        at=at,
        cwd=cwd,
        model_id=use.model,
    )


def parse(lines: Iterable[Line], session_id: str, context: Context | None = None, now: float = 0.0) -> Parsed:
    """`context` is accepted for symmetry with Codex; Claude records carry their own cwd and model."""
    uses: dict[str, _Use] = {}
    order: dict[str, Invocation] = {}
    newest: float | None = None
    for line in lines:
        record = line.record
        at = timestamp(record.get("timestamp"))
        if at is not None:
            newest = at if newest is None else max(newest, at)
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else ""
        model = message.get("model") if isinstance(message, dict) else None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                tool_use_id = block["id"]
                if tool_use_id in order:
                    continue
                use = _Use(
                    line.offset,
                    str(block.get("name", "")),
                    block.get("input"),
                    model if isinstance(model, str) else None,
                    str(cwd),
                    at,
                )
                uses[tool_use_id] = use
                inv = order[tool_use_id] = Invocation(line.offset, at)
                if use.name in EDIT_TOOLS and at is not None:
                    inv.barrier = _call(session_id, tool_use_id, use, CallStatus.UNKNOWN, "", at, use.cwd)
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                tool_use_id = block["tool_use_id"]
                use = uses.pop(tool_use_id, None)
                if use is None or at is None:
                    continue
                failed = block.get("is_error") is True
                order[tool_use_id].call = _call(
                    session_id,
                    tool_use_id,
                    use,
                    CallStatus.FAILED if failed else CallStatus.OK,
                    _text(block.get("content")) if failed else "",
                    at,
                    use.cwd,
                )
    calls, resume = settle(order.values(), newest, now)
    return Parsed(tuple(calls), resume)
