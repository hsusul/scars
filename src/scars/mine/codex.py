"""Codex rollouts: `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl`.

Each line is `{"timestamp", "type", "payload"}`. Two shapes of tool call are read:

- `response_item` `function_call` (`name`, `call_id`, JSON `arguments`) answered by a
  `function_call_output` with the same `call_id`, and `custom_tool_call` (`name`, `call_id`, `input`)
  answered by `custom_tool_call_output`. Shell tools (`shell`, `exec_command`, `shell_command`,
  `container.exec`) become `Bash` calls and `apply_patch` becomes an edit. The `exec` tool only wraps
  scripted calls whose commands appear as items below, so it is skipped rather than counted twice.
- `event_msg` `item_completed` with a `CommandExecution` item (`id`, `command`, `cwd`, `exit_code`,
  `aggregated_output`), and a `FileChange` item, which is an edit.

A call fails only when a non-zero exit code is visible, by the same rule as the hook adapter: an integer
`exit_code`, or an exit-code line in the output header. A call with no visible exit code has status
`unknown`. The model comes from the latest `turn_context`. The session id is the id at the end of the
file name, which is also the first `session_meta` id. A cwd given as a `file://` URL is decoded to its
path (`%20` becomes a space).

The cwd and model in force at the resume point are returned with the calls, and the next batch starts
from them, so calls read later keep the context set by lines an earlier batch consumed. Calls are emitted
in invocation order, with an `apply_patch` call emitted at its start even before its output (see
`calls.settle`).
"""

import json
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from scars.adapters.codex import exit_status, patch_paths
from scars.mine.calls import (
    EDIT_TOOLS,
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

SHELL_FUNCTIONS = frozenset({"shell", "exec_command", "shell_command", "container.exec"})
WRAPPER_TOOLS = frozenset({"exec"})
SHELLS = frozenset({"bash", "zsh", "sh"})
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def session_id_from_name(path: str) -> str:
    stem = os.path.basename(path).removesuffix(".jsonl")
    found = _SESSION.search(stem)
    return found.group(0) if found else stem


@dataclass(frozen=True)
class _Started:
    offset: int
    name: str
    args: object
    at: float | None
    cwd: str
    model: str | None


def directory(value: object) -> str | None:
    """A cwd as a path, or None when it is not a string."""
    if not isinstance(value, str):
        return None
    return unquote(urlsplit(value).path) if value.startswith("file://") else value


def command_text(command: object) -> str | None:
    """A command given as a string, or as argv; `["bash", "-lc", script]` is the script itself."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(a, str) for a in command) and command:
        if len(command) == 3 and os.path.basename(command[0]) in SHELLS and command[1] in ("-c", "-lc"):
            return command[2]
        return shlex.join(command)
    return None


def _output_text(output: object) -> str:
    if isinstance(output, list):
        return "".join(str(b.get("text", "")) for b in output if isinstance(b, dict))
    if isinstance(output, dict):
        return json.dumps(output)
    return output if isinstance(output, str) else ""


def _status(output: object) -> tuple[CallStatus, str]:
    text = _output_text(output)
    code: int | None
    try:
        decoded = json.loads(text) if text.startswith("{") else None
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and "output" in decoded:
        metadata = decoded.get("metadata")
        raw = metadata.get("exit_code") if isinstance(metadata, dict) else decoded.get("exit_code")
        code = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        text = str(decoded.get("output", ""))
    else:
        code, text = exit_status(text)
    if code is None:
        return CallStatus.UNKNOWN, ""
    return (CallStatus.FAILED, text) if code else (CallStatus.OK, "")


def _args(raw: object) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _make(session_id: str, **kwargs: object) -> Call:
    return make_call(harness=Harness.CODEX, session_id=session_id, **kwargs)  # type: ignore[arg-type]


def parse(lines: Iterable[Line], session_id: str, context: Context | None = None, now: float = 0.0) -> Parsed:
    started: dict[str, _Started] = {}
    order: list[Invocation] = []
    pending: dict[str, Invocation] = {}
    start = context or Context()
    model: str | None = start.model_id
    cwd = start.cwd
    contexts: list[tuple[int, Context]] = []
    """(offset, context in force before that line) at each line that changed it."""
    newest: float | None = None

    def add(offset: int, **kwargs: object) -> None:
        order.append(Invocation(offset, None, call=_make(session_id, **kwargs)))

    for line in lines:
        record = line.record
        at = timestamp(record.get("timestamp"))
        if at is not None:
            newest = at if newest is None else max(newest, at)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = record.get("type")
        ptype = payload.get("type")
        if kind in ("session_meta", "turn_context"):
            contexts.append((line.offset, Context(cwd, model)))
        if kind == "session_meta" and not cwd:
            cwd = directory(payload.get("cwd")) or cwd
        elif kind == "turn_context":
            model = payload["model"] if isinstance(payload.get("model"), str) else model
            cwd = directory(payload.get("cwd")) or cwd
        elif kind == "event_msg" and ptype == "item_completed" and at is not None:
            item = payload.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            if item.get("type") == "CommandExecution":
                command = command_text(item.get("command"))
                if command is None:
                    continue
                code = item.get("exit_code")
                code = code if isinstance(code, int) and not isinstance(code, bool) else None
                status = CallStatus.UNKNOWN if code is None else CallStatus.FAILED if code else CallStatus.OK
                output = item.get("aggregated_output")
                add(
                    line.offset,
                    tool_use_id=item["id"],
                    tool="Bash",
                    raw_input=command,
                    command=command,
                    paths=(),
                    status=status,
                    output=output if isinstance(output, str) else "",
                    at=at,
                    cwd=directory(item.get("cwd")) or cwd,
                    model_id=model,
                )
            elif item.get("type") == "FileChange":
                changes = item.get("changes")
                add(
                    line.offset,
                    tool_use_id=item["id"],
                    tool="apply_patch",
                    raw_input=sorted(changes) if isinstance(changes, dict) else None,
                    command=None,
                    paths=tuple(sorted(changes)) if isinstance(changes, dict) else (),
                    status=CallStatus.UNKNOWN,
                    output="",
                    at=at,
                    cwd=cwd,
                    model_id=model,
                )
        elif kind == "response_item" and ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            call_id = payload.get("call_id")
            if not isinstance(name, str) or not isinstance(call_id, str) or name in WRAPPER_TOOLS:
                continue
            raw = payload.get("arguments") if ptype == "function_call" else payload.get("input")
            if call_id in started or call_id in pending:
                continue
            began = started[call_id] = _Started(line.offset, name, _args(raw), at, cwd, model)
            inv = pending[call_id] = Invocation(line.offset, at)
            order.append(inv)
            if name in EDIT_TOOLS and at is not None:
                inv.barrier = _edit(session_id, call_id, began, CallStatus.UNKNOWN, "", at)
        elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            begun = started.pop(call_id, None) if isinstance(call_id, str) else None
            if begun is None or at is None:
                continue
            inv = pending.pop(call_id)
            status, output = _status(payload.get("output"))
            args = begun.args
            if begun.name in SHELL_FUNCTIONS:
                found = args if isinstance(args, dict) else {}
                command = command_text(found.get("command", found.get("cmd")))
                if command is None:
                    inv.dropped = True
                    continue
                workdir = directory(found.get("workdir"))
                inv.call = _make(
                    session_id,
                    tool_use_id=call_id,
                    tool="Bash",
                    raw_input=command,
                    command=command,
                    paths=(),
                    status=status,
                    output=output,
                    at=at,
                    cwd=workdir or begun.cwd,
                    model_id=begun.model,
                )
            else:
                inv.call = _edit(session_id, call_id, begun, status, output, at)
    calls, resume = settle(order, newest, now)
    if resume is None:
        return Parsed(tuple(calls), None, Context(cwd, model))
    before = [c for offset, c in contexts if offset >= resume]
    return Parsed(tuple(calls), resume, before[0] if before else Context(cwd, model))


def _edit(session_id: str, call_id: str, begun: _Started, status: CallStatus, output: str, at: float) -> Call:
    """A non-shell call, such as `apply_patch`."""
    args = begun.args
    patch = args if isinstance(args, str) else ""
    return _make(
        session_id,
        tool_use_id=call_id,
        tool=begun.name,
        raw_input=args,
        command=None,
        paths=patch_paths(patch) if begun.name == "apply_patch" else (),
        status=status,
        output=output,
        at=at,
        cwd=begun.cwd,
        model_id=begun.model,
    )
