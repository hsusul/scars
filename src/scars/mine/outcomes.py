"""Delayed outcomes for hook hits, computed from mined calls.

Each `shown` hit at the plan or repair stage gets a reference call. For a repair hit it is the failed
call that fired the hit (looked up by tool use id). For a plan hit it is the first call in the session,
at or after the hit, that matches the hit's pre-trigger: the command or path trigger named by the hit's
`trigger_key` when that is the trigger that fired, else any of the lesson's command or path triggers,
whatever their stage (a mined lesson's command pre-trigger lives at the gate stage). Paths resolve
against the worktree root of each call's working directory. Then the first rule that holds decides:

1. `ignored_then_failed`: the lesson's error signature fails a call within the 5 calls after the
   reference call, or, for a plan hit, the reference call itself.
2. `fired_unneeded`: a plan hit whose reference call succeeded.
3. `heeded`: a repair hit whose next call of the same tool kind succeeded or no longer matches the
   lesson's pre-trigger. A lesson without a pre-trigger is heeded only by a success.
4. `no_attempt`: a plan hit whose session ended with no call matching the pre-trigger.
5. `unknown`: the session ended before any rule could be decided, or the window closed and no rule holds.

Rules 2 to 5 wait until the 5 calls after the reference call exist or the session has ended, so a later
failure always wins and the answer does not depend on when the miner ran. Transcripts mark no session
end, so a session has ended once it has been idle for 24 hours: no mined call and no hit of any kind in
that session for that long. It is decided once per session, from its latest activity.
Gate hits and hits that delivered nothing get no delayed outcome.
"""

import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from scars import store
from scars.match import InvalidTrigger, RecordedCall, call_matches
from scars.model import DelayedOutcome, Lesson, LessonId, Stage, ToolKind, Trigger, TriggerKind

WINDOW = 5
IDLE_SECONDS = 24 * 3600
DAY = 86400.0
BACKFILL_DAYS = 30
IGNORED_KEEP_SECONDS = 7 * DAY


@dataclass(frozen=True)
class HitRow:
    seq: int
    at: float
    session_id: str
    tool_use_id: str | None
    stage: Stage
    lesson_id: LessonId
    trigger_key: str | None = None


def _fired(trigger_key: str | None) -> Trigger | None:
    """The command or path trigger a `kind:stage:spec` key names, or None for any other key."""
    parts = (trigger_key or "").split(":", 2)
    if len(parts) != 3 or parts[0] not in (TriggerKind.COMMAND, TriggerKind.PATH):
        return None
    try:
        return Trigger(TriggerKind(parts[0]), parts[2], Stage(parts[1]))
    except ValueError:
        return None


Roots = Callable[[str], str | None]


def _no_root(cwd: str) -> str | None:
    return None


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


def recorded(call: store.MinedCall, repo_root: str | None = None) -> RecordedCall:
    return RecordedCall(
        tool_kind=ToolKind(call.tool_kind),
        command=call.command,
        paths=call.paths,
        succeeded=call.status == "ok",
        error_signature=call.error_signature,  # type: ignore[arg-type]
        cwd=call.cwd,
        repo_root=repo_root,
    )


def _matches_pre(facts: LessonFacts, call: store.MinedCall, root_of: Roots) -> bool:
    needs_root = call.paths and any(t.kind is TriggerKind.PATH for t in facts.pre_triggers)
    record = recorded(call, root_of(call.cwd) if needs_root else None)
    for trigger in facts.pre_triggers:
        try:
            if call_matches(trigger, record):
                return True
        except InvalidTrigger:
            continue
    return False


def _failed_with(facts: LessonFacts, call: store.MinedCall) -> bool:
    return facts.signature is not None and call.status == "failed" and call.error_signature == facts.signature


def decide(
    hit: HitRow,
    facts: LessonFacts,
    calls: Sequence[store.MinedCall],
    ended: bool,
    root_of: Roots = _no_root,
) -> DelayedOutcome | None:
    """The delayed outcome of one hit given its session's calls in order, or None while undecided.

    `root_of` maps a call's working directory to its worktree root, for path triggers.
    """
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
        if following.status == "ok" or (facts.pre_triggers and not _matches_pre(facts, following, root_of)):
            return DelayedOutcome.HEEDED
        return DelayedOutcome.UNKNOWN
    fired = _fired(hit.trigger_key)
    if fired is not None:
        facts = replace(facts, pre_triggers=(fired,))
    index = next((i for i, c in enumerate(calls) if c.at >= hit.at and _matches_pre(facts, c, root_of)), None)
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


def run(lessons: sqlite3.Connection, telemetry_path: str, now: float, *, root_of: Roots = _no_root) -> Result:
    """Write the outcomes that can be decided and backfill missing model ids. Skips a missing or
    unusable telemetry.db. `root_of` maps a working directory to its worktree root."""
    if not os.path.exists(telemetry_path):
        return Result(0, 0)
    try:
        tdb = sqlite3.connect(telemetry_path, isolation_level=None, timeout=2)
    except sqlite3.Error:
        return Result(0, 0)
    try:
        store.ensure_telemetry_schema(tdb)
        # The hook writes one ignored row per event it skips; nothing reads them after doctor's day.
        tdb.execute("delete from hits where outcome = 'ignored' and at < ?", (now - IGNORED_KEEP_SECONDS,))
        return _run(lessons, tdb, now, root_of)
    except sqlite3.Error:
        return Result(0, 0)
    finally:
        tdb.close()


def _run(lessons: sqlite3.Connection, tdb: sqlite3.Connection, now: float, root_of: Roots) -> Result:
    rows = tdb.execute(
        "select seq, at, session_id, tool_use_id, stage, lesson_id, trigger_key from hits h "
        "where outcome = 'shown' "
        "and lesson_id is not null and stage in ('plan','repair') "
        "and not exists (select 1 from outcomes o where o.hit_seq = h.seq) order by seq"
    ).fetchall()
    sessions: dict[str, list[store.MinedCall]] = {}

    def calls_of(session_id: str) -> list[store.MinedCall]:
        if session_id not in sessions:
            sessions[session_id] = store.session_calls(lessons, session_id)
        return sessions[session_id]

    hit_activity = dict(tdb.execute("select session_id, max(at) from hits group by session_id").fetchall())

    def ended(session_id: str) -> bool:
        latest = max([hit_activity.get(session_id, 0.0), *(c.at for c in calls_of(session_id))])
        return now - latest >= IDLE_SECONDS

    facts: dict[str, LessonFacts] = {}
    decided: list[tuple[int, str]] = []
    for seq, at, session_id, tool_use_id, stage, lesson_id, trigger_key in rows:
        hit = HitRow(seq, at, session_id, tool_use_id, Stage(stage), LessonId(lesson_id), trigger_key)
        if lesson_id not in facts:
            facts[lesson_id] = LessonFacts.of(store.get_lesson(lessons, hit.lesson_id))
        stream_session = session_id
        if hit.stage is Stage.REPAIR and tool_use_id:
            found = store.mined_call(lessons, tool_use_id)
            stream_session = found.session_id if found else session_id
        over = ended(session_id) and ended(stream_session)
        outcome = decide(hit, facts[lesson_id], calls_of(stream_session), over, root_of)
        if outcome is not None:
            decided.append((seq, str(outcome)))
    models: list[tuple[str, int]] = []
    # Mined calls are kept for 30 days, so an older hit has nothing left to take its model from.
    backfill_since = now - BACKFILL_DAYS * DAY
    for seq, session_id, at in tdb.execute(
        "select seq, session_id, at from hits where model_id is null and at >= ?", (backfill_since,)
    ):
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
