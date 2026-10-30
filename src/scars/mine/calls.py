"""The typed tool call both transcript readers produce, and the incremental file reader they share."""

import json
import os
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
    """The transcript shows no outcome, as for a Codex call without a visible exit code. Such a call
    counts toward the pairing window but is neither a failure nor a fix."""


@dataclass(frozen=True)
class Call:
    harness: Harness
    session_id: str
    tool_use_id: str
    tool: str
    """The harness tool name. Shell calls are `Bash` and Codex edits are `apply_patch` in both harnesses."""
    tool_kind: ToolKind
    input: str
    """The shell command for shell calls, else the tool input as compact JSON, capped at 4 KB."""
    program: str | None
    """For shell calls, the program of the first simple command, or None when the parser rejects it."""
    paths: tuple[str, ...]
    status: CallStatus
    output: str
    """Output text of a failed call, capped at 4 KB; empty otherwise."""
    at: float
    cwd: str
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


def tool_kind(tool: str) -> ToolKind:
    if tool == SHELL_TOOL:
        return ToolKind.SHELL
    return ToolKind.EDIT if tool in EDIT_TOOLS else ToolKind.OTHER


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
    if kind is ToolKind.SHELL and command is not None:
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
