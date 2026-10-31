"""Domain types shared by every module."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType

LessonId = NewType("LessonId", str)
RepoId = NewType("RepoId", str)
SessionId = NewType("SessionId", str)
ErrorSignature = NewType("ErrorSignature", str)

TEXT_CAP = 240


class Status(StrEnum):
    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    PROMOTED = "promoted"
    RETIRED = "retired"
    SUPERSEDED = "superseded"


ACTIVE_STATUSES = frozenset({Status.CANDIDATE, Status.PROVISIONAL, Status.PROMOTED})


class Level(StrEnum):
    NOTE = "note"
    WARN = "warn"
    BLOCK = "block"


class TrustClass(StrEnum):
    OWN_CODE = "own_code"
    THIRD_PARTY_REPO = "third_party_repo"
    WEB = "web"
    HUMAN = "human"


class Stage(StrEnum):
    PLAN = "plan"
    GATE = "gate"
    REPAIR = "repair"


class TriggerKind(StrEnum):
    COMMAND = "command"
    PATH = "path"
    ERROR = "error"
    PROMPT = "prompt"


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class HookEventKind(StrEnum):
    SESSION_START = "session_start"
    COMPACT = "compact"
    PROMPT = "prompt"
    PRE_TOOL = "pre_tool"
    TOOL_FAILED = "tool_failed"


class ToolKind(StrEnum):
    SHELL = "shell"
    EDIT = "edit"
    OTHER = "other"


class HitOutcome(StrEnum):
    SHOWN = "shown"
    BLOCKED = "blocked"
    NO_MATCH = "no_match"
    LOG_ONLY = "log_only"
    ERROR = "error"
    IGNORED = "ignored"
    """A well-formed event scars does not act on; no lessons were read."""


class DelayedOutcome(StrEnum):
    HEEDED = "heeded"
    IGNORED_THEN_FAILED = "ignored_then_failed"
    FIRED_UNNEEDED = "fired_unneeded"
    NO_ATTEMPT = "no_attempt"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    PROPOSED = "proposed"
    OBSERVED = "observed"
    PROVISIONALIZED = "provisionalized"
    EXPIRED = "expired"
    PROMOTED = "promoted"
    PINNED = "pinned"
    UNPINNED = "unpinned"
    LEVEL_SET = "level_set"
    EDITED = "edited"
    MUTED = "muted"
    RETIRED = "retired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Transition:
    allowed_from: frozenset[Status]
    to: Status | None
    """Target status. None keeps the current status (for example `observed` or `pinned`)."""


LIFECYCLE: dict[EventType, Transition] = {
    EventType.PROPOSED: Transition(frozenset(), Status.CANDIDATE),
    EventType.OBSERVED: Transition(ACTIVE_STATUSES, None),
    EventType.PROVISIONALIZED: Transition(frozenset({Status.CANDIDATE}), Status.PROVISIONAL),
    EventType.EXPIRED: Transition(frozenset({Status.CANDIDATE, Status.PROVISIONAL}), Status.RETIRED),
    EventType.PROMOTED: Transition(frozenset({Status.CANDIDATE, Status.PROVISIONAL}), Status.PROMOTED),
    EventType.PINNED: Transition(frozenset({Status.PROMOTED}), None),
    EventType.UNPINNED: Transition(frozenset({Status.PROMOTED}), None),
    EventType.LEVEL_SET: Transition(frozenset({Status.PROMOTED}), None),
    EventType.EDITED: Transition(ACTIVE_STATUSES, Status.CANDIDATE),
    EventType.MUTED: Transition(ACTIVE_STATUSES, Status.RETIRED),
    EventType.RETIRED: Transition(ACTIVE_STATUSES, Status.RETIRED),
    EventType.SUPERSEDED: Transition(ACTIVE_STATUSES, Status.SUPERSEDED),
}


@dataclass(frozen=True)
class MachineScope:
    kind: Literal["machine"] = "machine"


@dataclass(frozen=True)
class RepoScope:
    repo_id: RepoId
    kind: Literal["repo"] = "repo"


@dataclass(frozen=True)
class PathScope:
    repo_id: RepoId
    glob: str
    kind: Literal["path"] = "path"


@dataclass(frozen=True)
class SessionScope:
    session_id: SessionId
    kind: Literal["session"] = "session"


Scope = MachineScope | RepoScope | PathScope | SessionScope


@dataclass(frozen=True)
class LessonText:
    avoid: str
    instead: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("avoid", "instead", "reason"):
            value = getattr(self, name)
            if not value or len(value) > TEXT_CAP:
                raise ValueError(f"{name} must be 1 to {TEXT_CAP} characters, got {len(value)}")


@dataclass(frozen=True)
class Trigger:
    kind: TriggerKind
    spec: str
    stage: Stage


@dataclass(frozen=True)
class Lesson:
    id: LessonId
    revision: int
    status: Status
    level: Level
    pinned: bool
    scope: Scope
    text: LessonText
    trust_class: TrustClass
    triggers: tuple[Trigger, ...]
    fingerprint: str
    sessions_seen: int


@dataclass(frozen=True)
class HookEvent:
    harness: Harness
    harness_version: str
    event: HookEventKind
    session_id: SessionId
    tool_use_id: str | None
    cwd: str
    tool_kind: ToolKind | None
    command: str | None
    paths: tuple[str, ...]
    error_text: str | None
    prompt: str | None
    model_id: str | None


PRIVILEGED_COMMANDS = frozenset(
    {
        "promote",
        "pin",
        "unpin",
        "level",
        "retire",
        "mute",
        "supersede",
        "off",
        "on",
        "import-guard",
        "verify",
        "quarantine",
    }
)
"""Commands that change a lesson's reach, level or status, or toggle the kill switch. Agents may not run them.
"""
