"""Trigger grammar, matching, stage selection and the precision check.

No user-supplied regular expression is ever compiled: specs parse into the small typed patterns below,
so a hostile spec cannot cause catastrophic backtracking.
"""

import bisect
import fnmatch
import functools
import posixpath
import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping
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
_Fixed = ExactArg | PrefixArg | OneArg

_DOUBLE_DASH = "--"


@dataclass(frozen=True)
class CommandPattern:
    """A `command` spec, matched against one simple command.

    Grammar, with tokens split on spaces:

    - The first token is the program, compared with the command's basename: exact, or a prefix when it
      ends in `*`.
    - `!FLAG` requires that FLAG is not one of the arguments before the command's first `--`.
    - Every other token is an argument token: `*` matches exactly one argument, `**` matches any run of
      arguments including none, a token ending in `*` matches one argument with that prefix, and any
      other token matches one argument with exactly that text.
    - Argument tokens are anchored at the first argument and matched in order, one argument each, with
      no gaps. Only `**` skips a run and `*` skips one. Arguments after the last token are free. So
      `git push --force` matches `git push --force origin` but not `git log push --force` or
      `git -C x push --force`, which needs `git ** push --force`. `xcodebuild -scheme *` does not match
      `xcodebuild -workspace W -scheme X`, which needs `xcodebuild ** -scheme *`.
    - The command's first `--` ends its options. An exact or prefix token matches an argument after it
      only when the pattern has its own `--` token earlier, and a pattern `--` token matches only the
      command's first `--`. So `git push --force` never matches `git log -- push --force`, while
      `git checkout -- *` matches `git checkout -- a.txt`. `*` and `**` may match any argument.
    - With no argument tokens, any arguments match.

    The shell decides some arguments at run time (a variable, a glob, a `~`). Such an argument may become
    no words, several words, or `--`, so it and every argument after it are unknown. No token except
    `**` and the free tail can match them, and a `!FLAG` check fails, so the pattern does not match,
    unless a known `--` comes before the first unknown argument.
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
    def anchored(self) -> bool:
        return not (self.args and isinstance(self.args[0], AnyArgs))

    @functools.cached_property
    def runs(self) -> tuple[tuple[tuple[_Fixed, bool], ...], ...]:
        """Argument tokens split on `**` into fixed-length runs of (token, after a pattern `--`).

        Matching each run after the first at its leftmost allowed position is exact: whether a token may
        sit at a position depends only on that position, so an earlier placement never leaves less room
        for the runs that follow.
        """
        runs: list[tuple[tuple[_Fixed, bool], ...]] = []
        current: list[tuple[_Fixed, bool]] = []
        after_dash = False
        for token in self.args:
            if isinstance(token, AnyArgs):
                runs.append(tuple(current))
                current = []
                continue
            current.append((token, after_dash))
            if token == ExactArg(_DOUBLE_DASH):
                after_dash = True
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


@dataclass(frozen=True)
class _Indexed:
    """A simple command plus lookups built once per parse and shared by every lesson.

    Only the known arguments, those before the first one the shell expands at run time, are indexed.
    """

    command: SimpleCommand
    known: tuple[str, ...]
    positions: dict[str, tuple[int, ...]]
    sorted_pairs: tuple[tuple[str, int], ...]
    dash: int | None
    """Index of the first `--` among the known arguments, or None."""
    options_known: bool
    """Every argument before the command's first `--` is known, so a `!FLAG` check can be decided."""

    @classmethod
    def of(cls, command: SimpleCommand) -> "_Indexed":
        known = command.args[: min(command.uncertain, default=len(command.args))]
        positions: dict[str, list[int]] = {}
        for i, arg in enumerate(known):
            positions.setdefault(arg, []).append(i)
        pairs = tuple(sorted((arg, i) for i, arg in enumerate(known)))
        dash = positions[_DOUBLE_DASH][0] if _DOUBLE_DASH in positions else None
        options_known = dash is not None or len(known) == len(command.args)
        return cls(command, known, {a: tuple(p) for a, p in positions.items()}, pairs, dash, options_known)

    def has_prefix(self, prefix: str) -> bool:
        lo = bisect.bisect_left(self.sorted_pairs, (prefix,))
        return lo < len(self.sorted_pairs) and self.sorted_pairs[lo][0].startswith(prefix)

    def has_option(self, flag: str) -> bool:
        end = len(self.known) if self.dash is None else self.dash
        return any(i < end for i in self.positions.get(flag, ()))

    def starts(self, token: _Fixed, start: int) -> Iterable[int]:
        """Ascending known positions at or after `start` where `token`'s text matches."""
        match token:
            case ExactArg(text):
                return (i for i in self.positions.get(text, ()) if i >= start)
            case PrefixArg(text):
                lo = bisect.bisect_left(self.sorted_pairs, (text,))
                found: list[int] = []
                for arg, i in self.sorted_pairs[lo:]:
                    if not arg.startswith(text):
                        break
                    if i >= start:
                        found.append(i)
                return sorted(found)
            case OneArg():
                return range(start, len(self.known))

    def fits(self, token: _Fixed, after_dash: bool, i: int) -> bool:
        """Whether `token` may match the known argument at `i`, by text and by its side of `--`."""
        arg = self.known[i]
        match token:
            case OneArg():
                return True
            case ExactArg(text) if text == _DOUBLE_DASH and not after_dash:
                return i == self.dash
            case ExactArg(text):
                text_ok = arg == text
            case PrefixArg(text):
                text_ok = arg.startswith(text)
        if after_dash:
            return text_ok and self.dash is not None and i > self.dash
        return text_ok and (self.dash is None or i < self.dash)


def command_matches(pattern: CommandPattern, command: SimpleCommand) -> bool:
    return _command_matches(pattern, _Indexed.of(command))


def _command_matches(pattern: CommandPattern, indexed: _Indexed) -> bool:
    if not _name_matches(pattern.program, indexed.command.program):
        return False
    if pattern.absent and not indexed.options_known:
        return False
    if any(indexed.has_option(flag) for flag in pattern.absent):
        return False
    if not all(text in indexed.positions for text in pattern.literals):
        return False
    if not all(indexed.has_prefix(prefix) for prefix in pattern.prefixes):
        return False
    position = 0
    for k, run in enumerate(pattern.runs):
        if k == 0 and pattern.anchored:
            found = 0 if _run_fits(run, indexed, 0) else None
        else:
            found = _find_run(run, indexed, position)
        if found is None:
            return False
        position = found + len(run)
    return True


def _name_matches(token: ExactArg | PrefixArg, program: str) -> bool:
    match token:
        case ExactArg(text):
            return program == text
        case PrefixArg(text):
            return program.startswith(text)


def _run_fits(run: tuple[tuple[_Fixed, bool], ...], indexed: _Indexed, i: int) -> bool:
    if i + len(run) > len(indexed.known):
        return False
    return all(indexed.fits(token, after, i + j) for j, (token, after) in enumerate(run))


def _find_run(run: tuple[tuple[_Fixed, bool], ...], indexed: _Indexed, start: int) -> int | None:
    """Leftmost position at or after `start` where the whole run matches."""
    last = len(indexed.known) - len(run)
    for i in indexed.starts(run[0][0], start):
        if i > last:
            return None
        if _run_fits(run, indexed, i):
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


def _matching_commands(pattern: CommandPattern, command: str | None) -> tuple[_Indexed, ...]:
    commands = _parsed(command) if command is not None else None
    return tuple(c for c in commands or () if _command_matches(pattern, c))


def _any_command(pattern: CommandPattern, command: str | None) -> bool:
    return bool(_matching_commands(pattern, command))


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
    scores: Mapping[LessonId, float] | None = None,
    shown: Collection[tuple[LessonId, str]] = frozenset(),
    deadline_ms: int = 50,
    clock: Callable[[], float] = time.monotonic,
) -> Selection:
    """Eligible lessons for the event's stage, best first, capped by count, under a wall-clock deadline.

    Order: higher score first, then promoted before provisional before candidate, then lesson id. With a
    `scores` mapping, a lesson's score is its entry, or 0 when it has none; without one, it is
    `sessions_seen`. A path-scoped lesson is admitted only where its glob applies, as `_located` says.
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

    def rank(le: Lesson) -> tuple[float, int, str]:
        score = float(le.sessions_seen) if scores is None else scores.get(le.id, 0.0)
        return (-score, _STATUS_RANK.get(le.status, 3), le.id)

    for lesson in sorted(lessons, key=rank):
        if _eligible(rule, lesson, event, ctx):
            hit = _fire(rule, lesson, event, paths, ctx, seen)
            if hit is not None:
                chosen.append(hit)
        if clock() > limit:
            return Selection(tuple(chosen), True)
        if rule.cap is not None and len(chosen) >= rule.cap:
            break
    return Selection(tuple(chosen), False)


def _in_scope(scope: Scope, event: HookEvent, ctx: MatchContext) -> bool:
    """The scope check that needs no trigger. A path scope also needs a location, checked by `_located`."""
    match scope:
        case MachineScope():
            return True
        case RepoScope(repo_id=repo_id):
            return ctx.repo_id is not None and repo_id == ctx.repo_id
        case PathScope(repo_id=repo_id):
            return ctx.repo_root is not None and ctx.repo_id is not None and repo_id == ctx.repo_id
        case SessionScope(session_id=session_id):
            return session_id == event.session_id


def _located(
    scope: Scope, trigger: Trigger | None, event: HookEvent, paths: tuple[str, ...], ctx: MatchContext
) -> bool:
    """Whether a path-scoped lesson applies where this event happens. Other scopes always pass.

    The candidate locations are the event's paths, the working directory of each simple command the
    trigger matched (for a command trigger, when the parser knows it), and the session cwd. Each is
    resolved against the session cwd, made relative to the repo root, and admits the lesson when it
    matches the scope glob. A relative location with no absolute session cwd, a location outside the
    repo or at its root, and a missing repo root admit nothing.
    """
    if not isinstance(scope, PathScope):
        return True
    root = ctx.repo_root
    if root is None:
        return False
    try:
        pattern = parse_spec(TriggerKind.PATH, scope.glob)
    except InvalidTrigger:
        return False
    assert isinstance(pattern, PathPattern)
    locations = [*paths, event.cwd]
    if trigger is not None and trigger.kind is TriggerKind.COMMAND:
        command = parse_spec(trigger.kind, trigger.spec)
        assert isinstance(command, CommandPattern)
        for c in _matching_commands(command, event.command):
            if c.command.cwd_known:
                locations.append(posixpath.join(event.cwd, c.command.cwd or ""))
    for location in locations:
        full = location if location.startswith("/") else None
        if full is None and event.cwd.startswith("/"):
            full = posixpath.join(event.cwd, location)
        rel = repo_relative(full, "", root) if full is not None else None
        if rel is not None and path_matches(pattern, rel):
            return True
    return False


def _eligible(rule: StageRule, lesson: Lesson, event: HookEvent, ctx: MatchContext) -> bool:
    session_candidate = rule.session_candidates and lesson.status is Status.CANDIDATE
    session_candidate = session_candidate and isinstance(lesson.scope, SessionScope)
    return (
        (lesson.status in rule.statuses or session_candidate)
        and (not rule.pinned_only or lesson.pinned)
        and (rule.level is None or lesson.level is rule.level)
        and lesson.scope.kind in rule.scope_kinds
        and _in_scope(lesson.scope, event, ctx)
    )


def _fire(
    rule: StageRule,
    lesson: Lesson,
    event: HookEvent,
    paths: tuple[str, ...],
    ctx: MatchContext,
    seen: Collection[tuple[LessonId, str]],
) -> Selected | None:
    """The first trigger that fires, has not been shown, and leaves the lesson in scope, or None."""
    if rule.trigger_kinds is None:
        if (lesson.id, PLAN_START_KEY) in seen or not _located(lesson.scope, None, event, paths, ctx):
            return None
        return Selected(lesson, None, PLAN_START_KEY)
    for trigger in lesson.triggers:
        if trigger.kind not in rule.trigger_kinds or trigger.stage is not rule.stage:
            continue
        key = trigger_key(trigger)
        if (lesson.id, key) in seen:
            continue
        try:
            fired = matches(trigger, event, ctx)
        except InvalidTrigger:
            continue
        if fired and _located(lesson.scope, trigger, event, paths, ctx):
            return Selected(lesson, trigger, key)
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

    @property
    def warns_broad(self) -> bool:
        """`scars add` warns that the trigger is broad: it matched more than 5% of all calls."""
        return self.total > 0 and 100 * self.matched > 5 * self.total


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
