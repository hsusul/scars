"""The pair rule: which failed calls have a fix.

A failed call pairs with the first later call in the same session and the same place that succeeds with
the same tool and, for shell calls, the same program, provided it comes within the next 10 calls and no
edit call (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `apply_patch`) comes before it. An edit means
the fix was a source change, which is ordinary debugging and not a lesson.

The same place means the same registered repo, or the same working directory when neither directory is
in a registered repo. A lesson takes its scope and trust from the passing call's repo, so a failure in an
upstream clone fixed by a call in an own repo would otherwise come out trusted as own code. A failure
whose working directory is unknown (`cwd` None) never pairs.

A failure still undecided when the batch ends stays open: its count of calls seen so far is carried to
the next batch. It is dropped once it has seen 10 calls, or once its session has been idle for 24 hours
(measured between calls, and against the miner's clock for a session with no new calls).

Only failures with a lessonable error signature open. A failed edit call never opens, because the next
edit call would end its window before it could pair. A failed shell call whose command the shell parser
rejects never opens either: its program is unknown, so no later call can be shown to run the same one.
"""

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, replace

from scars.mine.calls import EDIT_TOOLS, Call, CallStatus
from scars.model import ErrorSignature, ToolKind
from scars.signature import signature

WINDOW = 10
IDLE_SECONDS = 24 * 3600


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


Place = Callable[[str], Hashable]
"""Maps a working directory to where it is: its registered repo, or the directory itself."""


def same_directory(cwd: str) -> Hashable:
    return cwd


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


def _fixes(failure: OpenFailure, call: Call, place: Place) -> bool:
    return (
        call.status is CallStatus.OK
        and call.tool == failure.tool
        and call.program == failure.program
        and failure.cwd is not None
        and place(failure.cwd) == place(call.cwd)
    )


def pair_session(
    waiting: Iterable[OpenFailure], calls: Sequence[Call], now: float, place: Place = same_directory
) -> Paired:
    """Advance one session's open failures over its new calls, in order, and open the new failures."""
    active = list(waiting)
    pairs: list[Pair] = []
    failures = unlessonable = dropped = 0
    for call in calls:
        kept: list[OpenFailure] = []
        for failure in active:
            if call.at - failure.last_call_at >= IDLE_SECONDS or call.tool in EDIT_TOOLS:
                dropped += 1
            elif _fixes(failure, call, place):
                pairs.append(Pair(failure, call))
            elif failure.calls_since + 1 >= WINDOW:
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
    return Paired(tuple(pairs), still, failures, unlessonable, dropped + len(active) - len(still))
