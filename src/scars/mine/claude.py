"""Claude Code transcripts: `~/.claude/projects/<project>/<session>.jsonl`.

Assistant records carry `tool_use` blocks (`id`, `name`, `input`) in `message.content`, with the model in
`message.model`. User records carry the matching `tool_result` blocks (`tool_use_id`, `is_error`,
`content`), with `timestamp` and `cwd` on the record. A call is complete when its result is read; its
time is the result's time. The session id is the file name, as in hook stdin.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from scars.mine.calls import SHELL_TOOL, Call, CallStatus, Line, Parsed, make_call, timestamp
from scars.model import Harness

UNRESOLVED_HORIZON = 24 * 3600
"""A tool call with no result this long after the newest record is abandoned (an interrupted session),
so it no longer holds the file's offset back."""


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


def parse(lines: Iterable[Line], session_id: str) -> Parsed:
    uses: dict[str, _Use] = {}
    calls: list[Call] = []
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
                uses[block["id"]] = _Use(
                    line.offset,
                    str(block.get("name", "")),
                    block.get("input"),
                    model if isinstance(model, str) else None,
                    str(cwd),
                    at,
                )
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                use = uses.pop(block["tool_use_id"], None)
                if use is None or at is None:
                    continue
                failed = block.get("is_error") is True
                args = use.input
                command = args.get("command") if isinstance(args, dict) else None
                calls.append(
                    make_call(
                        harness=Harness.CLAUDE,
                        session_id=session_id,
                        tool_use_id=block["tool_use_id"],
                        tool=use.name,
                        raw_input=args,
                        command=command if use.name == SHELL_TOOL and isinstance(command, str) else None,
                        paths=_paths(args),
                        status=CallStatus.FAILED if failed else CallStatus.OK,
                        output=_text(block.get("content")) if failed else "",
                        at=at,
                        cwd=str(cwd) or use.cwd,
                        model_id=use.model,
                    )
                )
    waiting = [
        u.offset
        for u in uses.values()
        if newest is None or u.at is None or newest - u.at < UNRESOLVED_HORIZON
    ]
    return Parsed(tuple(calls), min(waiting) if waiting else None)
