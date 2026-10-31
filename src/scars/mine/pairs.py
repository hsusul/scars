"""The pair rule: which failed calls have a fix.

A failed call pairs with the first later call in the same session and the same place that succeeds with
the same tool and, for shell calls, the same program and the same target, provided it comes within the
next 10 calls and no edit call (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `apply_patch`) comes before
it. An edit means the fix was a source change, which is ordinary debugging and not a lesson.

The same target means the first simple command of each call has the same subcommand, the same
positional operands and the same value for every option that selects the target, so only other options
and their values differ. A pass of the same program on another file, PR, branch, make target, directory,
repo or namespace is a different task, not a fix. The subcommand is the first words that are neither
options nor option values, as many as `SUBCOMMAND_DEPTH` gives for the program (or for the program and its
first such word), compared in order. Make targets and `xcodebuild` actions are operands.

Options are classified from two small per-program tables. `TARGET_OPTIONS` lists options whose value names
the target (`git -C`, `gh -R`, `kubectl -n`, `npm --prefix`); when both calls give one, the values must be
equal, while adding one to a call that had none may be the fix itself. `VALUE_OPTIONS` lists options known
to take a value that may differ (`--limit`, `head -n`, `pytest -k`). The word right after any other option
written without `=` may be that option's value or an operand, since the miner does not know the option, so
it is never a subcommand word but must match like an operand: `git push origin main` and
`git push --force origin main` match, while `rm -f a` and `rm -f b` do not. Words compare as written, so
an argument the shell expands at run time matches only the same text (an unquoted `repos/o/r?ref=x` still
matches its quoted retry). Operands are compared in order, since `cp a b` and `cp b a` act on different
files, except for the subcommands in `UNORDERED`, whose operands name distinct roles.

A pass of the same tool and program in the same place that runs another target is counted as an
`operand_mismatch` skip, and the failure keeps waiting for its fix within the same window.

For a non-shell tool (built-in or MCP), the same target means the inputs differ in at most
`render.TOOL_MAX_FIELDS` top-level fields when both are JSON objects (a field added, removed or changed
counts once). A pass that differs in more is counted as a `structured_too_many_fields` skip and the failure
keeps waiting, like an operand mismatch: it is another task, not a fix. Inputs that are not both JSON
objects still pair, and the builder names why they yield no lesson.

The same place means the same registered repo, or the same working directory when neither directory is
in a registered repo. A lesson takes its scope and trust from the passing call's repo, so a failure in an
upstream clone fixed by a call in an own repo would otherwise come out trusted as own code. A call whose
place is unknown (an empty, relative or deleted working directory) never pairs.

A failure still undecided when the batch ends stays open: its count of calls seen so far is carried to
the next batch. It is dropped once it has seen 10 calls, or once its session has been idle for 24 hours
(measured between calls, and against the miner's clock for a session with no new calls).

Only failures with a lessonable error signature open. A failed edit call never opens, because the next
edit call would end its window before it could pair. A failed shell call whose command the shell parser
rejects never opens either: its program is unknown, so no later call can be shown to run the same one.
"""

import json
import posixpath
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, replace

from scars import render
from scars.mine.calls import EDIT_TOOLS, Call, CallStatus
from scars.model import ErrorSignature, ToolKind
from scars.shell import Unsupported, parse
from scars.signature import signature

WINDOW = 10
IDLE_SECONDS = 24 * 3600

SUBCOMMAND_DEPTH: dict[tuple[str, ...], int] = {
    ("git",): 1,
    ("git", "worktree"): 2,
    ("git", "stash"): 2,
    ("git", "remote"): 2,
    ("git", "submodule"): 2,
    ("gh",): 2,
    ("npm",): 1,
    ("pnpm",): 1,
    ("yarn",): 1,
    ("cargo",): 1,
    ("uv",): 1,
    ("pip",): 1,
    ("docker",): 1,
    ("kubectl",): 1,
    ("xcrun", "simctl"): 2,
}
"""How many leading non-option words name the subcommand, keyed by the program's base name alone or with
its first such word. The longer key wins; a program in neither has no subcommand."""

TARGET_OPTIONS: dict[str, dict[str, str]] = {
    "git": {"-C": "-C", "--git-dir": "--git-dir", "--work-tree": "--work-tree"},
    "gh": {"-R": "--repo", "--repo": "--repo"},
    "npm": {"--prefix": "--prefix", "-w": "--workspace", "--workspace": "--workspace"},
    "pnpm": {"-C": "--dir", "--dir": "--dir", "--filter": "--filter", "-F": "--filter"},
    "yarn": {"--cwd": "--cwd"},
    "uv": {"--directory": "--directory", "--project": "--project"},
    "xcodebuild": {"-project": "-project", "-workspace": "-workspace", "-scheme": "-scheme"},
    "make": {"-C": "--directory", "--directory": "--directory", "-f": "--file", "--file": "--file"},
    "cargo": {"--manifest-path": "--manifest-path", "-p": "--package", "--package": "--package"},
    "kubectl": {
        "-n": "--namespace",
        "--namespace": "--namespace",
        "--context": "--context",
        "--kubeconfig": "--kubeconfig",
        "-l": "--selector",
        "--selector": "--selector",
    },
    "docker": {"-H": "--host", "--host": "--host", "-c": "--context", "--context": "--context"},
}
"""Per program, the spellings of options whose value selects the target, mapped to one name per option."""

VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {"-m", "--message", "-n", "--max-count", "--grep", "--since", "--until", "--author", "-c"}
    ),
    "gh": frozenset({"--limit", "-L", "--json", "--jq", "-q", "--template", "--state", "-s", "--search"}),
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "pytest": frozenset({"-k", "-m", "-n", "-p", "--tb", "--maxfail"}),
    "make": frozenset({"-j", "--jobs"}),
    "cargo": frozenset({"-j", "--jobs"}),
    "rg": frozenset({"-A", "-B", "-C", "-m", "--max-count"}),
    "grep": frozenset({"-A", "-B", "-C", "-m", "--max-count"}),
    "kubectl": frozenset({"-o", "--output", "--tail", "--since"}),
    "docker": frozenset({"--tail", "--since", "--format"}),
}
"""Per program, options written without `=` that are known to take the next word as a value that may
differ between a failure and its fix."""

_VALUE = "value"
_UNKNOWN = "unknown"
"""What the word after a bare option is when the option is not in `TARGET_OPTIONS`; selector names start
with `-`, so neither clashes with one."""

UNORDERED: frozenset[tuple[str, ...]] = frozenset({("git", "worktree", "add")})
"""Program and subcommand whose operands may come in any order and still name the same target."""


@dataclass(frozen=True)
class Target:
    """What a shell command acts on: its program, subcommand, positional operands and selecting options."""

    program: str
    subcommand: tuple[str, ...]
    operands: tuple[str, ...]
    """In order, including the word right after an option that is in neither option table."""
    selectors: dict[str, tuple[str, ...]]
    """The values of the options in `TARGET_OPTIONS`, by option name."""


def _is_option(arg: str) -> bool:
    return arg.startswith("-") and arg != "-"


def target(command: str) -> Target | None:
    """The target of the command's first simple command, or None when the parser rejects it."""
    try:
        commands = parse(command)
    except Unsupported:
        return None
    if not commands:
        return None
    simple = commands[0]
    program = posixpath.basename(simple.program)
    selecting = TARGET_OPTIONS.get(program, {})
    valued = VALUE_OPTIONS.get(program, frozenset())
    words: list[tuple[str, bool]] = []
    selectors: dict[str, list[str]] = {}
    options_end = False
    pending: str | None = None
    for arg in simple.args:
        if not options_end and arg == "--":
            options_end, pending = True, None
        elif not options_end and _is_option(arg):
            name, eq, value = arg.partition("=")
            pending = None
            if name in selecting and eq:
                selectors.setdefault(selecting[name], []).append(value)
            elif name in selecting:
                pending = selecting[name]
            elif arg[:2] in selecting:
                selectors.setdefault(selecting[arg[:2]], []).append(arg[2:])
            elif not eq:
                pending = _VALUE if name in valued else _UNKNOWN
        elif pending is None:
            words.append((arg, False))
        else:
            if pending == _UNKNOWN:
                words.append((arg, True))
            elif pending != _VALUE:
                selectors.setdefault(pending, []).append(arg)
            pending = None
    plain = [word for word, ambiguous in words if not ambiguous]
    depth = SUBCOMMAND_DEPTH.get((program, *plain[:1]), SUBCOMMAND_DEPTH.get((program,), 0))
    subcommand: list[str] = []
    operands: list[str] = []
    for word, ambiguous in words:
        (subcommand if not ambiguous and len(subcommand) < depth else operands).append(word)
    return Target(program, tuple(subcommand), tuple(operands), {k: tuple(v) for k, v in selectors.items()})


def same_target(failed: str, passing: str) -> bool:
    """Whether two shell commands act on the same target, so only options that do not select it differ."""
    a, b = target(failed), target(passing)
    if a is None or b is None or (a.program, a.subcommand) != (b.program, b.subcommand):
        return False
    if any(a.selectors[name] != b.selectors[name] for name in a.selectors.keys() & b.selectors.keys()):
        return False
    if (a.program, *a.subcommand) in UNORDERED:
        return Counter(a.operands) == Counter(b.operands)
    return a.operands == b.operands


def input_object(text: str) -> dict[str, object] | None:
    """A non-shell tool input as its top-level JSON object, or None when it is not one (or was cut)."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def differing_keys(failed: dict[str, object], passing: dict[str, object]) -> tuple[str, ...]:
    """The top-level keys added, removed or changed, sorted. Values compare as JSON, so `1` and `true`
    differ."""

    def same(key: str) -> bool:
        return key in failed and key in passing and _json(failed[key]) == _json(passing[key])

    return tuple(sorted(k for k in failed.keys() | passing.keys() if not same(k)))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


@dataclass(frozen=True)
class OpenFailure:
    tool_use_id: str
    harness: str
    session_id: str
    tool: str
    program: str | None
    input: str
    signature: ErrorSignature
    calls_since: int
    last_call_at: float
    cwd: str | None


Place = Callable[[str], Hashable | None]
"""Maps a working directory to where it is (its registered repo, or the directory itself), or None when
that is unknown."""


def same_directory(cwd: str) -> Hashable | None:
    return cwd or None


@dataclass(frozen=True)
class Pair:
    failed: OpenFailure
    passing: Call


@dataclass(frozen=True)
class Paired:
    pairs: tuple[Pair, ...]
    still_open: tuple[OpenFailure, ...]
    failures: int
    """Failed calls among the new calls."""
    unlessonable: int
    """Failed calls skipped: no lessonable signature, an edit tool, or an unparseable shell command."""
    dropped: int
    """Open failures closed without a fix: an edit came first, the window ran out, or the session idled."""
    operand_mismatch: int = 0
    """Passes skipped as a fix because they ran the failed program on another target."""
    too_many_fields: int = 0
    """Passes of the failed non-shell tool skipped as a fix because too many input fields differ."""


def open_failure(call: Call) -> OpenFailure | None:
    """The open failure a failed call starts, or None when it can never yield a lesson."""
    if call.status is not CallStatus.FAILED or call.tool in EDIT_TOOLS:
        return None
    if call.tool_kind is ToolKind.SHELL and call.program is None:
        return None
    sig = signature(call.output, command=call.command)
    if sig is None:
        return None
    return OpenFailure(
        tool_use_id=call.tool_use_id,
        harness=str(call.harness),
        session_id=call.session_id,
        tool=call.tool,
        program=call.program,
        input=call.input,
        signature=sig,
        calls_since=0,
        last_call_at=call.at,
        cwd=call.cwd,
    )


_OPERAND = "operand_mismatch"
_FIELDS = "structured_too_many_fields"


def _fixes(failure: OpenFailure, call: Call, place: Place) -> bool | str:
    """True when the call fixes the failure, the skip name when it would but acts on another target,
    else False."""
    candidate = (
        call.status is CallStatus.OK
        and call.tool == failure.tool
        and call.program == failure.program
        and failure.cwd is not None
        and (where := place(failure.cwd)) is not None
        and where == place(call.cwd)
    )
    if not candidate:
        return False
    if call.tool_kind is ToolKind.SHELL:
        return True if same_target(failure.input, call.input) else _OPERAND
    failed, passing = input_object(failure.input), input_object(call.input)
    too_many = (
        failed is not None
        and passing is not None
        and len(differing_keys(failed, passing)) > render.TOOL_MAX_FIELDS
    )
    return _FIELDS if too_many else True


def pair_session(
    waiting: Iterable[OpenFailure], calls: Sequence[Call], now: float, place: Place = same_directory
) -> Paired:
    """Advance one session's open failures over its new calls, in order, and open the new failures."""
    active = list(waiting)
    pairs: list[Pair] = []
    failures = unlessonable = dropped = 0
    mismatched: Counter[str] = Counter()
    for call in calls:
        kept: list[OpenFailure] = []
        for failure in active:
            if call.at - failure.last_call_at >= IDLE_SECONDS or call.tool in EDIT_TOOLS:
                dropped += 1
                continue
            fixes = _fixes(failure, call, place)
            if fixes is True:
                pairs.append(Pair(failure, call))
                continue
            if fixes:
                mismatched[fixes] += 1
            if failure.calls_since + 1 >= WINDOW:
                dropped += 1
            else:
                kept.append(replace(failure, calls_since=failure.calls_since + 1, last_call_at=call.at))
        active = kept
        if call.status is CallStatus.FAILED:
            failures += 1
            opened = open_failure(call)
            if opened is None:
                unlessonable += 1
            else:
                active.append(opened)
    still = tuple(f for f in active if now - f.last_call_at < IDLE_SECONDS)
    return Paired(
        tuple(pairs),
        still,
        failures,
        unlessonable,
        dropped + len(active) - len(still),
        mismatched[_OPERAND],
        mismatched[_FIELDS],
    )
