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
file name, which is also the first `session_meta` id.
"""

import json
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from scars.adapters.codex import exit_status, patch_paths
from scars.mine.calls import Call, CallStatus, Line, Parsed, make_call, timestamp
from scars.model import Harness

SHELL_FUNCTIONS = frozenset({"shell", "exec_command", "shell_command", "container.exec"})
WRAPPER_TOOLS = frozenset({"exec"})
SHELLS = frozenset({"bash", "zsh", "sh"})
UNRESOLVED_HORIZON = 24 * 3600
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


def parse(lines: Iterable[Line], session_id: str) -> Parsed:
    started: dict[str, _Started] = {}
    calls: list[Call] = []
    model: str | None = None
    cwd = ""
    newest: float | None = None

    def add(**kwargs: object) -> None:
        calls.append(make_call(harness=Harness.CODEX, session_id=session_id, **kwargs))  # type: ignore[arg-type]

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
        if kind == "session_meta" and not cwd and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]
        elif kind == "turn_context":
            model = payload["model"] if isinstance(payload.get("model"), str) else model
            cwd = payload["cwd"] if isinstance(payload.get("cwd"), str) else cwd
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
                    tool_use_id=item["id"],
                    tool="Bash",
                    raw_input=command,
                    command=command,
                    paths=(),
                    status=status,
                    output=output if isinstance(output, str) else "",
                    at=at,
                    cwd=item["cwd"] if isinstance(item.get("cwd"), str) else cwd,
                    model_id=model,
                )
            elif item.get("type") == "FileChange":
                changes = item.get("changes")
                add(
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
            started[call_id] = _Started(line.offset, name, _args(raw), at, cwd, model)
        elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            start = started.pop(call_id, None) if isinstance(call_id, str) else None
            if start is None or at is None:
                continue
            status, output = _status(payload.get("output"))
            args = start.args
            if start.name in SHELL_FUNCTIONS:
                found = args if isinstance(args, dict) else {}
                command = command_text(found.get("command", found.get("cmd")))
                if command is None:
                    continue
                workdir = found.get("workdir")
                add(
                    tool_use_id=call_id,
                    tool="Bash",
                    raw_input=command,
                    command=command,
                    paths=(),
                    status=status,
                    output=output,
                    at=at,
                    cwd=workdir if isinstance(workdir, str) else start.cwd,
                    model_id=start.model,
                )
            else:
                patch = args if isinstance(args, str) else ""
                add(
                    tool_use_id=call_id,
                    tool=start.name,
                    raw_input=args,
                    command=None,
                    paths=patch_paths(patch) if start.name == "apply_patch" else (),
                    status=status,
                    output=output,
                    at=at,
                    cwd=start.cwd,
                    model_id=start.model,
                )
    waiting = [
        s.offset
        for s in started.values()
        if newest is None or s.at is None or newest - s.at < UNRESOLVED_HORIZON
    ]
    return Parsed(tuple(calls), min(waiting) if waiting else None)
