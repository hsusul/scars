"""The pair rule: which failed calls have a fix.

A failed call pairs with the first later call in the same session and the same place that succeeds with
the same tool and, for shell calls, the same program and the same target, provided it comes within the
next 10 calls and no edit call (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `apply_patch`) comes before
it. An edit means the fix was a source change, which is ordinary debugging and not a lesson.

The same target means the first simple command of each call has the same subcommand and the same
positional operands, so only options and option values differ. A pass of the same program on another
file, PR, branch or make target is a different task, not a fix. The subcommand is the first words that are
neither options nor option values, as many as `SUBCOMMAND_DEPTH` gives for the program (or for the program
and its first such word), compared in order. Make targets and `xcodebuild` actions are operands. A word
right after an option written without `=` may be that option's value or an operand, since the miner knows
no program's options, so it is never a subcommand word, and for the operands each call's
operands must appear among the other call's operands and such words, so `git push origin main` and
`git push --force origin main` match while `sed -n 1,240p a` and `sed -n 1,180p b` do not. Words compare as
written, so an argument the shell expands at run time matches only the same text (an unquoted
`repos/o/r?ref=x` still matches its quoted retry). Operands are compared as a multiset, because their
order rarely names a different target.

A pass of the same tool and program in the same place that runs another target is counted as an
`operand_mismatch` skip, and the failure keeps waiting for its fix within the same window.

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

import posixpath
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, replace

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


@dataclass(frozen=True)
class Target:
    """What a shell command acts on: its subcommand and positional operands."""

    subcommand: tuple[str, ...]
    operands: Counter[str]
    option_words: Counter[str]
    """Words right after an option written without `=`: its value, or an operand."""


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
    words: list[str] = []
    option_words: Counter[str] = Counter()
    options_end = after_option = False
    for arg in simple.args:
        if not options_end and arg == "--":
            options_end, after_option = True, False
        elif not options_end and _is_option(arg):
            after_option = "=" not in arg
        elif after_option:
            option_words[arg] += 1
            after_option = False
        else:
            words.append(arg)
    program = posixpath.basename(simple.program)
    depth = SUBCOMMAND_DEPTH.get((program, *words[:1]), SUBCOMMAND_DEPTH.get((program,), 0))
    return Target(tuple(words[:depth]), Counter(words[depth:]), option_words)


def _covered(operands: Counter[str], other: Target) -> bool:
    return not operands - other.operands - other.option_words


def same_target(failed: str, passing: str) -> bool:
    """Whether two shell commands act on the same target, so only options and option values differ."""
    a, b = target(failed), target(passing)
    if a is None or b is None or a.subcommand != b.subcommand:
        return False
    return _covered(a.operands, b) and _covered(b.operands, a)


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


def open_failure(call: Call) -> OpenFailure | None:
    """The open failure a failed call starts, or None when it can never yield a lesson."""
    if call.status is not CallStatus.FAILED or call.tool in EDIT_TOOLS:
        return None
    if call.tool_kind is ToolKind.SHELL and call.program is None:
        return None
    sig = signature(call.output)
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


def _fixes(failure: OpenFailure, call: Call, place: Place) -> bool | None:
    """True when the call fixes the failure, None when it would but runs another target, else False."""
    candidate = (
        call.status is CallStatus.OK
        and call.tool == failure.tool
        and call.program == failure.program
        and failure.cwd is not None
        and (where := place(failure.cwd)) is not None
        and where == place(call.cwd)
    )
    if not candidate or call.tool_kind is not ToolKind.SHELL:
        return candidate
    return True if same_target(failure.input, call.input) else None


def pair_session(
    waiting: Iterable[OpenFailure], calls: Sequence[Call], now: float, place: Place = same_directory
) -> Paired:
    """Advance one session's open failures over its new calls, in order, and open the new failures."""
    active = list(waiting)
    pairs: list[Pair] = []
    failures = unlessonable = dropped = mismatched = 0
    for call in calls:
        kept: list[OpenFailure] = []
        for failure in active:
            if call.at - failure.last_call_at >= IDLE_SECONDS or call.tool in EDIT_TOOLS:
                dropped += 1
                continue
            fixes = _fixes(failure, call, place)
            if fixes:
                pairs.append(Pair(failure, call))
                continue
            if fixes is None:
                mismatched += 1
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
    return Paired(tuple(pairs), still, failures, unlessonable, dropped + len(active) - len(still), mismatched)
