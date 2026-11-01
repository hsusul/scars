"""Typed Claude Code tool calls and the incremental transcript file reader."""

import json
import os
import posixpath
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from scars.model import Harness, ToolKind
from scars.shell import Unsupported, parse

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
SHELL_TOOL = "Bash"
OUTPUT_CAP = 4096
INPUT_CAP = 4096


class CallStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    UNKNOWN = "unknown"
    """The transcript shows no outcome. Such a call counts toward the pairing window but is neither a
    failure nor a fix."""


@dataclass(frozen=True)
class Call:
    harness: Harness
    session_id: str
    tool_use_id: str
    tool: str
    """The Claude Code tool name; shell calls are `Bash`."""
    tool_kind: ToolKind
    input: str
    """The shell command for shell calls, else the tool input as compact JSON, capped at 4 KB. For
    `cd <dir> && <command>` it is the command alone (see `cd_yield`)."""
    program: str | None
    """For shell calls, the program of the first simple command, or None when the parser rejects it."""
    paths: tuple[str, ...]
    status: CallStatus
    output: str
    """Output text of a failed call, capped at 4 KB; empty otherwise."""
    at: float
    cwd: str
    """The directory the call ran in; empty when unknown."""
    model_id: str | None

    @property
    def command(self) -> str | None:
        return self.input if self.tool_kind is ToolKind.SHELL else None


@dataclass(frozen=True)
class Line:
    offset: int
    """Byte offset of the line's first byte in the file."""
    record: dict[str, object]


@dataclass(frozen=True)
class Parsed:
    calls: tuple[Call, ...]
    resume_offset: int | None
    """Where the next batch must start reading, when a call's start was read but its result was not yet
    written; None means after the last complete line."""


UNRESOLVED_HORIZON = 24 * 3600
"""A tool call with no result this long after the newest record, or before the miner's clock, is abandoned
(an interrupted session), so it no longer holds the file's offset back."""


@dataclass
class Invocation:
    """One tool call in invocation order, resolved once its result is read."""

    offset: int
    """Byte offset of the line that started the call."""
    at: float | None
    call: Call | None = None
    """The finished call, set when its result is read."""
    barrier: Call | None = None
    """For an edit call: what to emit while its result is not yet read. An edit ends every open pair at
    the point it was invoked, whatever its result."""
    dropped: bool = False
    """Resolved, but into nothing the miner reads (a shell call whose command is not readable)."""


def settle(
    invocations: Iterable[Invocation], newest: float | None, now: float
) -> tuple[list[Call], int | None]:
    """The calls that can be emitted, in invocation order, and the offset to resume from.

    Emission stops at the first unresolved call that is not an edit and not abandoned: its result could
    still add a call to some failure's 10-call window, so no later call is final yet. Those later calls
    are read again by the next batch, starting at the blocking call's line.
    """
    out: list[Call] = []
    latest = max(now, newest) if newest is not None else now
    for inv in invocations:
        if inv.call is not None:
            out.append(inv.call)
        elif inv.barrier is not None:
            out.append(inv.barrier)
        elif inv.dropped or (inv.at is not None and latest - inv.at >= UNRESOLVED_HORIZON):
            continue
        else:
            return out, inv.offset
    return out, None


def tool_kind(tool: str) -> ToolKind:
    if tool == SHELL_TOOL:
        return ToolKind.SHELL
    return ToolKind.EDIT if tool in EDIT_TOOLS else ToolKind.OTHER


def cd_yield(command: str, cwd: str) -> tuple[str, str] | None:
    """For a command that is exactly `cd <literal dir> && <one plain simple command>`, that simple command
    (`shlex.join` of its words) and the directory it runs in, resolved against `cwd`: empty when the
    directory is relative and `cwd` is not absolute. None for every other command."""
    try:
        words = shlex.split(command)
        parsed = parse(command)
    except (ValueError, Unsupported):
        return None
    if len(words) < 4 or words[0] != "cd" or words[2] != "&&" or len(parsed) != 1:
        return None
    (simple,) = parsed
    plain = not simple.uncertain and simple.cwd_known and simple.cwd == words[1]
    if not plain or words[3:] != [simple.program, *simple.args]:
        return None
    target = words[1]
    if not target.startswith("/"):
        target = posixpath.join(cwd, target) if cwd.startswith("/") else ""
    return shlex.join(words[3:]), posixpath.normpath(target) if target else ""


def shell_program(command: str) -> str | None:
    try:
        commands = parse(command)
    except Unsupported:
        return None
    return commands[0].program if commands else None


def timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cap(text: str, limit: int) -> str:
    raw = text.encode()
    return text if len(raw) <= limit else raw[:limit].decode(errors="ignore")


def compact_json(value: object) -> str:
    return cap(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str), INPUT_CAP)


def make_call(
    *,
    harness: Harness,
    session_id: str,
    tool_use_id: str,
    tool: str,
    raw_input: object,
    command: str | None,
    paths: tuple[str, ...],
    status: CallStatus,
    output: str,
    at: float,
    cwd: str,
    model_id: str | None,
) -> Call:
    kind = tool_kind(tool)
    if kind is ToolKind.SHELL and command is not None and (yielded := cd_yield(command, cwd)) is not None:
        text, cwd = yielded
        program = shell_program(text)
    elif kind is ToolKind.SHELL and command is not None:
        text, program = command, shell_program(command)
    else:
        text, program = compact_json(raw_input), None
    return Call(
        harness=harness,
        session_id=session_id,
        tool_use_id=tool_use_id,
        tool=tool,
        tool_kind=kind,
        input=text,
        program=program,
        paths=paths,
        status=status,
        output=cap(output, OUTPUT_CAP) if status is CallStatus.FAILED else "",
        at=at,
        cwd=cwd,
        model_id=model_id,
    )


@dataclass(frozen=True)
class Chunk:
    lines: tuple[Line, ...]
    end: int
    """Offset just past the last complete line. A trailing line with no newline is never consumed."""
    malformed: int


def read_from(path: str, start: int) -> Chunk:
    """Every complete JSON-object line from byte `start` on."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    pieces = data.split(b"\n")
    lines: list[Line] = []
    malformed = 0
    pos = start
    for raw in pieces[:-1]:
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        if isinstance(record, dict):
            lines.append(Line(pos, record))
        elif raw.strip():
            malformed += 1
        pos += len(raw) + 1
    return Chunk(tuple(lines), pos, malformed)


@dataclass(frozen=True)
class FileState:
    inode: int
    size: int
    mtime: float


def file_state(path: str) -> FileState:
    st = os.stat(path)
    return FileState(st.st_ino, st.st_size, st.st_mtime)


def start_offset(saved_offset: int | None, saved_inode: int | None, state: FileState) -> int:
    """Resume at the saved offset, or at 0 when the file was replaced (new inode) or shrank."""
    if saved_offset is None or saved_inode != state.inode or state.size < saved_offset:
        return 0
    return saved_offset
