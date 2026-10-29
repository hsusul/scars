"""Trigger grammar, matching, stage selection and the precision check.

No user-supplied regular expression is ever compiled: specs parse into the small typed patterns below,
so a hostile spec cannot cause catastrophic backtracking.
"""

import bisect
import fnmatch
import functools
import re
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from scars.model import (
    ErrorSignature,
    HookEvent,
    HookEventKind,
    Lesson,
    LessonId,
    Level,
    MachineScope,
    PathScope,
    RepoId,
    RepoScope,
    Scope,
    SessionScope,
    Stage,
    Status,
    ToolKind,
    Trigger,
    TriggerKind,
)
from scars.shell import SimpleCommand, Unsupported, parse


class InvalidTrigger(ValueError):
    """A trigger spec does not follow the grammar for its kind."""


@dataclass(frozen=True)
class ExactArg:
    text: str


@dataclass(frozen=True)
class PrefixArg:
    text: str


@dataclass(frozen=True)
class OneArg:
    """`*`: exactly one argument."""


@dataclass(frozen=True)
class AnyArgs:
    """`**`: any run of arguments, including none."""


ArgToken = ExactArg | PrefixArg | OneArg | AnyArgs


@dataclass(frozen=True)
class CommandPattern:
    """A `command` spec, matched against one simple command.

    The first spec token is the program (exact, or a prefix when it ends in `*`). `!FLAG` tokens require
    that FLAG is absent from every argument position. The other tokens must match one contiguous run of
    arguments, in order, starting anywhere: `xcodebuild -scheme *` matches
    `xcodebuild -workspace W -scheme X -sdk Y`. Gaps between tokens are allowed only through `**`:
    `git commit --amend` does not match `git commit -m x --amend`, and `git commit ** --amend` does.
    With no argument tokens, any arguments match.
    """

    program: ExactArg | PrefixArg
    args: tuple[ArgToken, ...]
    absent: tuple[str, ...]

    @functools.cached_property
    def literals(self) -> frozenset[str]:
        """Exact arguments that must be present, for a cheap reject before scanning."""
        return frozenset(t.text for t in self.args if isinstance(t, ExactArg))

    @functools.cached_property
    def prefixes(self) -> frozenset[str]:
        return frozenset(t.text for t in self.args if isinstance(t, PrefixArg))

    @functools.cached_property
    def runs(self) -> tuple[tuple[ExactArg | PrefixArg | OneArg, ...], ...]:
        """Argument tokens split on `**` into fixed-length runs.

        Both ends are open, so matching each run at its leftmost position after the previous run is
        exact: an earlier placement never leaves less room for the runs that follow.
        """
        runs: list[tuple[ExactArg | PrefixArg | OneArg, ...]] = []
        current: list[ExactArg | PrefixArg | OneArg] = []
        for token in self.args:
            if isinstance(token, AnyArgs):
                runs.append(tuple(current))
                current = []
            else:
                current.append(token)
        runs.append(tuple(current))
        return tuple(run for run in runs if run)


@dataclass(frozen=True)
class PathPattern:
    """A `path` spec: an anchored, case-insensitive glob relative to the repo root.

    Segments split on `/`. A `**` segment matches zero or more whole segments. Other segments use
    `fnmatch` rules (`*`, `?`, `[...]`) within one segment, so `*` never crosses a `/`.
    """

    segments: tuple[str, ...]


@dataclass(frozen=True)
class ErrorPattern:
    signature: ErrorSignature


@dataclass(frozen=True)
class PromptPattern:
    """A `prompt` spec: space-separated lowercase keywords that must all appear as whole words."""

    keywords: frozenset[str]


Pattern = CommandPattern | PathPattern | ErrorPattern | PromptPattern

_WORD = re.compile(r"\w+")


@functools.lru_cache(maxsize=4096)
def parse_spec(kind: TriggerKind, spec: str) -> Pattern:
    """Parse a trigger spec into its typed pattern. Raises `InvalidTrigger` with a reason."""
    match kind:
        case TriggerKind.COMMAND:
            return _parse_command_spec(spec)
        case TriggerKind.PATH:
            return _parse_path_spec(spec)
        case TriggerKind.ERROR:
            if not spec.strip() or spec != spec.strip() or "\n" in spec:
                raise InvalidTrigger("an error spec must be one non-empty line with no outer whitespace")
            return ErrorPattern(ErrorSignature(spec))
        case TriggerKind.PROMPT:
            keywords = spec.split()
            if not keywords:
                raise InvalidTrigger("a prompt spec needs at least one keyword")
            for keyword in keywords:
                if keyword != keyword.lower() or not _WORD.fullmatch(keyword):
                    raise InvalidTrigger(f"prompt keyword {keyword!r} must be one lowercase word")
            return PromptPattern(frozenset(keywords))


def _parse_command_spec(spec: str) -> CommandPattern:
    tokens = spec.split()
    if not tokens:
        raise InvalidTrigger("a command spec needs a program")
    head, *rest = tokens
    program = _arg_token(head)
    if head.startswith("!") or not isinstance(program, ExactArg | PrefixArg) or not program.text:
        raise InvalidTrigger(f"the first token must be a program name, got {head!r}")
    if "/" in head:
        raise InvalidTrigger(f"the program is matched by basename, so {head!r} cannot contain '/'")
    args: list[ArgToken] = []
    absent: list[str] = []
    for token in rest:
        if token.startswith("!"):
            flag = token[1:]
            if not flag or "*" in flag or flag.startswith("!"):
                raise InvalidTrigger(f"{token!r} must be '!' followed by one literal flag")
            absent.append(flag)
        else:
            args.append(_arg_token(token))
    return CommandPattern(program, tuple(args), tuple(absent))


def _arg_token(token: str) -> ArgToken:
    if token == "*":
        return OneArg()
    if token == "**":
        return AnyArgs()
    if "*" in token[:-1]:
        raise InvalidTrigger(f"{token!r}: '*' is allowed only as '*', '**', or at the end of a token")
    if token.endswith("*"):
        return PrefixArg(token[:-1])
    return ExactArg(token)


def _parse_path_spec(spec: str) -> PathPattern:
    if not spec or spec.startswith("/"):
        raise InvalidTrigger("a path spec must be a non-empty glob relative to the repo root")
    segments = spec.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise InvalidTrigger(f"path spec {spec!r} has an empty, '.' or '..' segment")
        if "**" in segment and segment != "**":
            raise InvalidTrigger(f"path segment {segment!r}: '**' must be a whole segment")
    return PathPattern(tuple(s.lower() for s in segments))


def _arg_matches(token: ExactArg | PrefixArg | OneArg, arg: str) -> bool:
    match token:
        case ExactArg(text):
            return arg == text
        case PrefixArg(text):
            return arg.startswith(text)
        case OneArg():
            return True


@dataclass(frozen=True)
class _Indexed:
    """A simple command plus lookups built once per parse and shared by every lesson."""

    command: SimpleCommand
    positions: dict[str, tuple[int, ...]]
    sorted_pairs: tuple[tuple[str, int], ...]

    @classmethod
    def of(cls, command: SimpleCommand) -> "_Indexed":
        positions: dict[str, list[int]] = {}
        for i, arg in enumerate(command.args):
            positions.setdefault(arg, []).append(i)
        pairs = tuple(sorted((arg, i) for i, arg in enumerate(command.args)))
        return cls(command, {a: tuple(p) for a, p in positions.items()}, pairs)

    def _prefix_range(self, prefix: str) -> range:
        lo = bisect.bisect_left(self.sorted_pairs, (prefix,))
        hi = lo
        while hi < len(self.sorted_pairs) and self.sorted_pairs[hi][0].startswith(prefix):
            hi += 1
        return range(lo, hi)

    def has_prefix(self, prefix: str) -> bool:
        lo = bisect.bisect_left(self.sorted_pairs, (prefix,))
        return lo < len(self.sorted_pairs) and self.sorted_pairs[lo][0].startswith(prefix)

    def starts(self, token: ExactArg | PrefixArg | OneArg, start: int) -> Iterable[int]:
        """Ascending argument positions at or after `start` where `token` matches."""
        match token:
            case ExactArg(text):
                return (i for i in self.positions.get(text, ()) if i >= start)
            case PrefixArg(text):
                return sorted(i for k in self._prefix_range(text) if (i := self.sorted_pairs[k][1]) >= start)
            case OneArg():
                return range(start, len(self.command.args))


def command_matches(pattern: CommandPattern, command: SimpleCommand) -> bool:
    return _command_matches(pattern, _Indexed.of(command))


def _command_matches(pattern: CommandPattern, indexed: _Indexed) -> bool:
    command = indexed.command
    if not _arg_matches(pattern.program, command.program):
        return False
    if any(flag in indexed.positions for flag in pattern.absent):
        return False
    if not all(text in indexed.positions for text in pattern.literals):
        return False
    if not all(indexed.has_prefix(prefix) for prefix in pattern.prefixes):
        return False
    position = 0
    for run in pattern.runs:
        found = _find_run(run, indexed, position)
        if found is None:
            return False
        position = found + len(run)
    return True


def _find_run(run: tuple[ExactArg | PrefixArg | OneArg, ...], indexed: _Indexed, start: int) -> int | None:
    """Leftmost position at or after `start` where the whole run matches."""
    args = indexed.command.args
    last = len(args) - len(run)
    head, rest = run[0], run[1:]
    for i in indexed.starts(head, start):
        if i > last:
            return None
        if all(_arg_matches(token, args[i + 1 + j]) for j, token in enumerate(rest)):
            return i
    return None


def path_matches(pattern: PathPattern, repo_relative: str) -> bool:
    """Match a normalized repo-relative path (no leading `/`, no `..`) against the glob.

    Tracks every reachable segment position instead of backtracking, so cost is segments times parts.
    """
    parts = repo_relative.lower().split("/")
    positions = {0}
    for segment in pattern.segments:
        if segment == "**":
            positions = set(range(min(positions), len(parts) + 1))
        else:
            positions = {
                i + 1 for i in positions if i < len(parts) and fnmatch.fnmatchcase(parts[i], segment)
            }
        if not positions:
            return False
    return len(parts) in positions


@dataclass(frozen=True)
class MatchContext:
    """Facts the hook resolves once per event, outside matching.

    `repo_root` is the `git rev-parse --show-toplevel` path, already symlink-resolved by the caller.
    `error_signature` is `signature.signature(event.error_text)`, computed once by the caller.
    """

    repo_root: str | None = None
    repo_id: RepoId | None = None
    error_signature: ErrorSignature | None = None


@functools.lru_cache(maxsize=256)
def _parsed(command: str) -> tuple[_Indexed, ...] | None:
    """Parsed once per distinct command string, so 1,000 lessons share one parse."""
    try:
        return tuple(_Indexed.of(c) for c in parse(command))
    except Unsupported:
        return None


def _any_command(pattern: CommandPattern, command: str | None) -> bool:
    commands = _parsed(command) if command is not None else None
    return commands is not None and any(_command_matches(pattern, c) for c in commands)


def repo_relative(path: str, cwd: str, repo_root: str | None) -> str | None:
    """Lowercased path relative to the repo root, or None when it lies outside the root.

    Relative paths resolve against `cwd`. Without a repo root, only relative paths count, taken as
    repo-relative. Pure string work: the caller resolves symlinks.
    """
    if repo_root is None:
        if path.startswith("/"):
            return None
        rel = _normalize(path)
    else:
        root = _normalize(repo_root).lower()
        base = path if path.startswith("/") else f"{cwd or repo_root}/{path}"
        full = _normalize(base).lower()
        prefix = root.rstrip("/") + "/"
        if not full.startswith(prefix):
            return None
        rel = full[len(prefix) :]
    if rel in ("", ".") or rel == ".." or rel.startswith("../"):
        return None
    return rel.lower()


def _normalize(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
        else:
            parts.append(part)
    joined = "/".join(parts)
    return "/" + joined if path.startswith("/") else joined


def _any_path(pattern: PathPattern, paths: Iterable[str], cwd: str, repo_root: str | None) -> bool:
    for path in paths:
        rel = repo_relative(path, cwd, repo_root)
        if rel is not None and path_matches(pattern, rel):
            return True
    return False


_PATH_STRIP = "\"'`()[]{}<>,;:!?"


def prompt_paths(prompt: str) -> tuple[str, ...]:
    """Words in a prompt that look like paths: they contain `/` or `.` and are not URLs.

    Quotes, brackets and sentence punctuation are stripped from both ends until none is left, so
    "fix `src/app.py`." names `src/app.py`.
    """
    found: list[str] = []
    for word in prompt.split():
        while True:
            stripped = word.strip(_PATH_STRIP).rstrip(".")
            if stripped == word:
                break
            word = stripped
        if ("/" in word or "." in word) and "://" not in word:
            found.append(word)
    return tuple(found)


def event_paths(event: HookEvent) -> tuple[str, ...]:
    """Paths an event is about: its tool paths, plus the paths named in the prompt on prompt events."""
    named = prompt_paths(event.prompt) if event.event is HookEventKind.PROMPT and event.prompt else ()
    return (*event.paths, *named)


def matches(trigger: Trigger, event: HookEvent, context: MatchContext | None = None) -> bool:
    """Whether one trigger fires for one event. Raises `InvalidTrigger` for a malformed spec.

    A command trigger fires when any parsed simple command matches; an unsupported parse never fires.
    A path trigger checks `event.paths`, plus the paths named in the prompt on prompt events.
    """
    ctx = context or MatchContext()
    match parse_spec(trigger.kind, trigger.spec):
        case CommandPattern() as pattern:
            return _any_command(pattern, event.command)
        case PathPattern() as pattern:
            return _any_path(pattern, event_paths(event), event.cwd, ctx.repo_root)
        case ErrorPattern(signature):
            return ctx.error_signature is not None and ctx.error_signature == signature
        case PromptPattern(keywords):
            return event.prompt is not None and keywords <= set(_WORD.findall(event.prompt.lower()))


@dataclass(frozen=True)
class StageRule:
    """Which lessons one hook event may deliver. Character caps belong to rendering."""

    stage: Stage
    statuses: frozenset[Status]
    pinned_only: bool
    level: Level | None
    trigger_kinds: frozenset[TriggerKind] | None
    """Kinds that can fire at this stage. None means no matching: every eligible lesson is selected."""
    cap: int | None
    session_candidates: bool
    """Also admit candidates scoped to this session."""
    scope_kinds: frozenset[str] = frozenset({"machine", "repo", "path", "session"})
    dedupe: bool = True
    """Skip (lesson, trigger) pairs already shown. The gate never dedupes: every matching call is denied."""


_PLAN_START = StageRule(
    Stage.PLAN, frozenset({Status.PROMOTED}), True, None, None, None, False, frozenset({"machine", "repo"})
)

STAGE_RULES: dict[HookEventKind, StageRule] = {
    HookEventKind.SESSION_START: _PLAN_START,
    HookEventKind.COMPACT: _PLAN_START,
    HookEventKind.PROMPT: StageRule(
        Stage.PLAN,
        frozenset({Status.PROMOTED}),
        True,
        None,
        frozenset({TriggerKind.PROMPT, TriggerKind.PATH}),
        2,
        False,
    ),
    HookEventKind.PRE_TOOL: StageRule(
        Stage.GATE,
        frozenset({Status.PROMOTED}),
        False,
        Level.BLOCK,
        frozenset({TriggerKind.COMMAND, TriggerKind.PATH}),
        1,
        False,
        dedupe=False,
    ),
    HookEventKind.TOOL_FAILED: StageRule(
        Stage.REPAIR,
        frozenset({Status.PROMOTED, Status.PROVISIONAL}),
        False,
        None,
        frozenset({TriggerKind.ERROR}),
        2,
        True,
    ),
}

_STATUS_RANK = {Status.PROMOTED: 0, Status.PROVISIONAL: 1, Status.CANDIDATE: 2}


PLAN_START_KEY = "start:plan"
"""`trigger_key` for session start and compaction, where lessons are selected without a trigger."""


def trigger_key(trigger: Trigger) -> str:
    """Stable key for one trigger, as `kind:stage:spec`, used to record hits and dedupe deliveries."""
    return f"{trigger.kind.value}:{trigger.stage.value}:{trigger.spec}"


@dataclass(frozen=True)
class Selected:
    lesson: Lesson
    trigger: Trigger | None
    """The trigger that fired, or None at session start and compaction."""
    trigger_key: str


@dataclass(frozen=True)
class Selection:
    selected: tuple[Selected, ...]
    deadline_hit: bool
    """True when the deadline passed during the scan; `selected` holds the matches found by then."""

    @property
    def lessons(self) -> tuple[Lesson, ...]:
        return tuple(s.lesson for s in self.selected)


def select(
    lessons: Iterable[Lesson],
    event: HookEvent,
    context: MatchContext | None = None,
    *,
    shown: Collection[tuple[LessonId, str]] = frozenset(),
    deadline_ms: int = 50,
    clock: Callable[[], float] = time.monotonic,
) -> Selection:
    """Eligible lessons for the event's stage, best first, capped by count, under a wall-clock deadline.

    Order: promoted before provisional before candidate, then more `sessions_seen`, then lesson id.
    Only triggers whose `stage` equals the event's stage are evaluated. A trigger with a malformed spec
    is skipped, so one bad row cannot silence every other lesson.

    `shown` holds the `(lesson_id, trigger_key)` pairs already delivered in this session and context
    epoch. Those pairs are skipped before the cap is applied, so shown lessons do not crowd out unshown
    ones; a lesson whose first firing trigger was shown can still fire through another trigger. The
    gate ignores `shown`. The deadline is checked with `clock` after each lesson, with no signals.
    """
    ctx = context or MatchContext()
    rule = STAGE_RULES[event.event]
    seen = shown if rule.dedupe else frozenset()
    paths = event_paths(event)
    limit = clock() + deadline_ms / 1000
    chosen: list[Selected] = []
    ranked = sorted(lessons, key=lambda le: (_STATUS_RANK.get(le.status, 3), -le.sessions_seen, le.id))
    for lesson in ranked:
        if _eligible(rule, lesson, event, paths, ctx):
            hit = _fire(rule, lesson, event, ctx, seen)
            if hit is not None:
                chosen.append(hit)
        if clock() > limit:
            return Selection(tuple(chosen), True)
        if rule.cap is not None and len(chosen) >= rule.cap:
            break
    return Selection(tuple(chosen), False)


def _in_scope(scope: Scope, event: HookEvent, paths: tuple[str, ...], ctx: MatchContext) -> bool:
    match scope:
        case MachineScope():
            return True
        case RepoScope(repo_id=repo_id):
            return ctx.repo_id is not None and repo_id == ctx.repo_id
        case PathScope(repo_id=repo_id, glob=glob):
            if ctx.repo_id is None or repo_id != ctx.repo_id or not paths:
                return False
            try:
                pattern = parse_spec(TriggerKind.PATH, glob)
            except InvalidTrigger:
                return False
            return isinstance(pattern, PathPattern) and _any_path(pattern, paths, event.cwd, ctx.repo_root)
        case SessionScope(session_id=session_id):
            return session_id == event.session_id


def _eligible(
    rule: StageRule, lesson: Lesson, event: HookEvent, paths: tuple[str, ...], ctx: MatchContext
) -> bool:
    session_candidate = rule.session_candidates and lesson.status is Status.CANDIDATE
    session_candidate = session_candidate and isinstance(lesson.scope, SessionScope)
    return (
        (lesson.status in rule.statuses or session_candidate)
        and (not rule.pinned_only or lesson.pinned)
        and (rule.level is None or lesson.level is rule.level)
        and lesson.scope.kind in rule.scope_kinds
        and _in_scope(lesson.scope, event, paths, ctx)
    )


def _fire(
    rule: StageRule,
    lesson: Lesson,
    event: HookEvent,
    ctx: MatchContext,
    seen: Collection[tuple[LessonId, str]],
) -> Selected | None:
    """The first trigger that fires and has not been shown, as a selection, or None."""
    if rule.trigger_kinds is None:
        return None if (lesson.id, PLAN_START_KEY) in seen else Selected(lesson, None, PLAN_START_KEY)
    for trigger in lesson.triggers:
        if trigger.kind not in rule.trigger_kinds or trigger.stage is not rule.stage:
            continue
        key = trigger_key(trigger)
        if (lesson.id, key) in seen:
            continue
        try:
            if matches(trigger, event, ctx):
                return Selected(lesson, trigger, key)
        except InvalidTrigger:
            continue
    return None


@dataclass(frozen=True)
class RecordedCall:
    """One mined tool call, as the miner produces it for the precision check.

    `repo_root` is the worktree root the call ran in, so calls from several worktrees of one repo resolve
    their paths against their own root. When it is None, the caller's default root applies.
    """

    tool_kind: ToolKind
    command: str | None
    paths: tuple[str, ...]
    succeeded: bool
    error_signature: ErrorSignature | None
    cwd: str = ""
    repo_root: str | None = None


def call_matches(trigger: Trigger, call: RecordedCall, *, repo_root: str | None = None) -> bool:
    """Whether one trigger matches one recorded call. Raises `InvalidTrigger` for a malformed spec.

    A command trigger matches only shell calls. A path trigger matches only calls with paths, resolved
    against `call.repo_root`, or `repo_root` when the call has none. An error trigger matches a failed
    call with the same signature. Prompt triggers do not apply to tool calls and raise `ValueError`.
    """
    match parse_spec(trigger.kind, trigger.spec):
        case CommandPattern() as pattern:
            return call.tool_kind is ToolKind.SHELL and _any_command(pattern, call.command)
        case PathPattern() as pattern:
            root = call.repo_root if call.repo_root is not None else repo_root
            return bool(call.paths) and _any_path(pattern, call.paths, call.cwd, root)
        case ErrorPattern(signature):
            return not call.succeeded and call.error_signature == signature
        case PromptPattern():
            raise ValueError("prompt triggers do not apply to tool calls")


@dataclass(frozen=True)
class Precision:
    matched_failing: int
    """Matched calls that failed with the lesson's error signature."""
    matched_passing: int
    """Matched calls that succeeded."""
    matched: int
    """All matched calls, including failures with other signatures."""
    total: int
    """All calls replayed, for the `scars add` 5% warning."""

    @property
    def keeps_pre_trigger(self) -> bool:
        """The miner keeps a provisional lesson's pre-trigger: at most 4 passing matches per failing one."""
        return self.matched_passing <= 4 * self.matched_failing

    @property
    def allows_block(self) -> bool:
        """Level `block` is allowed: at least 3 failing matches and under 1% passing ones."""
        return self.matched_failing >= 3 and 100 * self.matched_passing < (
            self.matched_failing + self.matched_passing
        )


def precision(
    trigger: Trigger,
    calls: Iterable[RecordedCall],
    *,
    error_signature: ErrorSignature,
    repo_root: str | None = None,
) -> Precision:
    """Replay a `command` or `path` pre-trigger over recorded calls, using `call_matches` for each.

    `repo_root` is the default for calls that do not carry their own.
    """
    if trigger.kind not in (TriggerKind.COMMAND, TriggerKind.PATH):
        raise ValueError(f"precision applies to command and path triggers, not {trigger.kind}")
    failing = passing = matched = total = 0
    for call in calls:
        total += 1
        if not call_matches(trigger, call, repo_root=repo_root):
            continue
        matched += 1
        if call.succeeded:
            passing += 1
        elif call.error_signature == error_signature:
            failing += 1
    return Precision(failing, passing, matched, total)
