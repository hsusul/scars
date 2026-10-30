"""Delayed outcomes for hook hits, computed from mined calls.

Each `shown` hit at the plan or repair stage gets a reference call. For a repair hit it is the failed
call that fired the hit (looked up by tool use id). For a plan hit it is the first call in the session,
at or after the hit, that matches one of the lesson's command or path triggers. Then the first rule that
holds decides:

1. `ignored_then_failed`: the lesson's error signature fails a call within the 5 calls after the
   reference call, or, for a plan hit, the reference call itself.
2. `fired_unneeded`: a plan hit whose reference call succeeded.
3. `heeded`: a repair hit whose next call of the same tool kind succeeded or no longer matches the
   lesson's pre-trigger. A lesson without a pre-trigger is heeded only by a success.
4. `no_attempt`: a plan hit whose session ended with no call matching the pre-trigger.
5. `unknown`: the session ended before any rule could be decided, or the window closed and no rule holds.

Rules 2 to 5 wait until the 5 calls after the reference call exist or the session has ended, so a later
failure always wins and the answer does not depend on when the miner ran. Transcripts mark no session
end, so a session has ended once it has been idle for 24 hours: no mined call and no hit for that long.
Gate hits and hits that delivered nothing get no delayed outcome.
"""

import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from scars import store
from scars.match import InvalidTrigger, RecordedCall, call_matches
from scars.model import DelayedOutcome, Lesson, LessonId, Stage, ToolKind, Trigger, TriggerKind

WINDOW = 5
IDLE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class HitRow:
    seq: int
    at: float
    session_id: str
    tool_use_id: str | None
    stage: Stage
    lesson_id: LessonId


@dataclass(frozen=True)
class LessonFacts:
    signature: str | None
    pre_triggers: tuple[Trigger, ...]

    @classmethod
    def of(cls, lesson: Lesson | None) -> "LessonFacts":
        if lesson is None:
            return cls(None, ())
        errors = [t.spec for t in lesson.triggers if t.kind is TriggerKind.ERROR]
        pre = tuple(t for t in lesson.triggers if t.kind in (TriggerKind.COMMAND, TriggerKind.PATH))
        return cls(errors[0] if errors else lesson.text.reason, pre)


def recorded(call: store.MinedCall) -> RecordedCall:
    return RecordedCall(
        tool_kind=ToolKind(call.tool_kind),
        command=call.command,
        paths=call.paths,
        succeeded=call.status == "ok",
        error_signature=call.error_signature,  # type: ignore[arg-type]
        cwd=call.cwd,
    )


def _matches_pre(facts: LessonFacts, call: store.MinedCall) -> bool:
    for trigger in facts.pre_triggers:
        try:
            if call_matches(trigger, recorded(call)):
                return True
        except InvalidTrigger:
            continue
    return False


def _failed_with(facts: LessonFacts, call: store.MinedCall) -> bool:
    return facts.signature is not None and call.status == "failed" and call.error_signature == facts.signature


def decide(
    hit: HitRow, facts: LessonFacts, calls: Sequence[store.MinedCall], ended: bool
) -> DelayedOutcome | None:
    """The delayed outcome of one hit given its session's calls in order, or None while undecided."""
    if hit.stage is Stage.REPAIR:
        index = next((i for i, c in enumerate(calls) if c.tool_use_id == hit.tool_use_id), None)
        if index is None:
            return DelayedOutcome.UNKNOWN if ended else None
        if any(_failed_with(facts, c) for c in calls[index + 1 : index + 1 + WINDOW]):
            return DelayedOutcome.IGNORED_THEN_FAILED
        if len(calls) - index - 1 < WINDOW and not ended:
            return None
        kind = calls[index].tool_kind
        following = next((c for c in calls[index + 1 :] if c.tool_kind == kind), None)
        if following is None:
            return DelayedOutcome.UNKNOWN if ended else None
        if following.status == "ok" or (facts.pre_triggers and not _matches_pre(facts, following)):
            return DelayedOutcome.HEEDED
        return DelayedOutcome.UNKNOWN
    index = next((i for i, c in enumerate(calls) if c.at >= hit.at and _matches_pre(facts, c)), None)
    if index is None:
        return DelayedOutcome.NO_ATTEMPT if ended else None
    if any(_failed_with(facts, c) for c in calls[index : index + 1 + WINDOW]):
        return DelayedOutcome.IGNORED_THEN_FAILED
    if len(calls) - index - 1 < WINDOW and not ended:
        return None
    return DelayedOutcome.FIRED_UNNEEDED if calls[index].status == "ok" else DelayedOutcome.UNKNOWN


def _model_at(calls: Sequence[store.MinedCall], at: float) -> str | None:
    known = [c for c in calls if c.model_id]
    before = [c for c in known if c.at <= at]
    if before:
        return before[-1].model_id
    return known[0].model_id if known else None


@dataclass(frozen=True)
class Result:
    outcomes: int
    models: int


def run(lessons: sqlite3.Connection, telemetry_path: str, now: float) -> Result:
    """Write the outcomes that can be decided and backfill missing model ids. Skips a missing or
    unusable telemetry.db."""
    if not os.path.exists(telemetry_path):
        return Result(0, 0)
    try:
        tdb = sqlite3.connect(telemetry_path, isolation_level=None, timeout=2)
    except sqlite3.Error:
        return Result(0, 0)
    try:
        return _run(lessons, tdb, now)
    except sqlite3.Error:
        return Result(0, 0)
    finally:
        tdb.close()


def _run(lessons: sqlite3.Connection, tdb: sqlite3.Connection, now: float) -> Result:
    rows = tdb.execute(
        "select seq, at, session_id, tool_use_id, stage, lesson_id from hits h where outcome = 'shown' "
        "and lesson_id is not null and stage in ('plan','repair') "
        "and not exists (select 1 from outcomes o where o.hit_seq = h.seq) order by seq"
    ).fetchall()
    sessions: dict[str, list[store.MinedCall]] = {}

    def calls_of(session_id: str) -> list[store.MinedCall]:
        if session_id not in sessions:
            sessions[session_id] = store.session_calls(lessons, session_id)
        return sessions[session_id]

    facts: dict[str, LessonFacts] = {}
    decided: list[tuple[int, str]] = []
    for seq, at, session_id, tool_use_id, stage, lesson_id in rows:
        hit = HitRow(seq, at, session_id, tool_use_id, Stage(stage), LessonId(lesson_id))
        if lesson_id not in facts:
            facts[lesson_id] = LessonFacts.of(store.get_lesson(lessons, hit.lesson_id))
        stream_session = session_id
        if hit.stage is Stage.REPAIR and tool_use_id:
            found = store.mined_call(lessons, tool_use_id)
            stream_session = found.session_id if found else session_id
        calls = calls_of(stream_session)
        last = max([at, *(c.at for c in calls)])
        outcome = decide(hit, facts[lesson_id], calls, now - last >= IDLE_SECONDS)
        if outcome is not None:
            decided.append((seq, str(outcome)))
    models: list[tuple[str, int]] = []
    for seq, session_id, at in tdb.execute("select seq, session_id, at from hits where model_id is null"):
        model = _model_at(calls_of(session_id), at)
        if model:
            models.append((model, seq))
    if not decided and not models:
        return Result(0, 0)
    tdb.execute("begin immediate")
    try:
        tdb.executemany("insert or ignore into outcomes (hit_seq, outcome) values (?,?)", decided)
        tdb.executemany("update hits set model_id = ? where seq = ? and model_id is null", models)
        tdb.execute("commit")
    except BaseException:
        tdb.execute("rollback")
        raise
    return Result(len(decided), len(models))
