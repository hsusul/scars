"""`scars ls`, `scars why` and `scars test`: read-only views that agents may run too.

`ls --firing` keeps lessons shown at least once in the last 30 days of telemetry. `ls --stale` keeps
lessons that neither the miner nor any hook has seen in 30 days: no sighting and no shown hit.
`test` records its result as the precision run of the lesson's current revision, which is what level
`block` will require.

Every line they print passes the sanitizer and secret scan on its way out (see `CleanLines`), and each
stored value also goes through `shown`. Stored data that does not load (a lesson with an empty text, a
trigger spec that no longer parses, a hit with a malformed time) is reported, never raised.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime

from scars import hook, render, store, telemetry
from scars.commands import (
    Context,
    UserError,
    get_lesson,
    lessons_db,
    shown,
    shown_json,
    telemetry_db,
    unrenderable,
)
from scars.match import InvalidTrigger, Precision, precision
from scars.mine import known_dir
from scars.mine.outcomes import recorded
from scars.model import (
    ACTIVE_STATUSES,
    ErrorSignature,
    Harness,
    Lesson,
    LessonId,
    RepoId,
    Status,
    TriggerKind,
)

WINDOW_DAYS = 30
DAY = 86400.0
AVOID_WIDTH = 60
STATUS_CHOICES = (*(s.value for s in Status), "all")


def when(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")


def harness_name(value: str) -> str:
    return value if value in {h.value for h in Harness} else shown(value)


def row(lesson: Lesson) -> str:
    avoid = shown(lesson.text.avoid)
    if len(avoid) > AVOID_WIDTH:
        avoid = avoid[: AVOID_WIDTH - 3] + "..."
    pin = "pin" if lesson.pinned else "-"
    return (
        f"{shown(lesson.id):<11} {lesson.status:<11} {lesson.level:<4} {pin:<3} {lesson.sessions_seen:>4}  "
        f"{shown(render.scope_label(lesson.scope))}  {avoid}"
    )


STORED_DATA_ERRORS = (ValueError, TypeError, sqlite3.Error)


def _shown_counts(since: float) -> dict[LessonId, int]:
    tele = telemetry_db()
    if tele is None:
        return {}
    try:
        return telemetry.shown_counts(tele, since)
    except STORED_DATA_ERRORS as exc:
        raise UserError(f"cannot read telemetry.db: {shown(str(exc))}") from None
    finally:
        tele.close()


def cmd_ls(args: argparse.Namespace, ctx: Context) -> int:
    since = ctx.clock() - WINDOW_DAYS * DAY
    statuses = (
        None if args.status == "all" else ACTIVE_STATUSES if args.status is None else {Status(args.status)}
    )
    repo = RepoId(args.repo) if args.repo else None
    with lessons_db(readonly=True) as db:
        lessons, problems = store.read_lessons(db, statuses=statuses, repo_id=repo)
        refused = unrenderable(db, lessons)
        to_review = store.signature_reviews(db)
        try:
            last_seen = store.lessons_last_seen(db) if args.stale else {}
        except (ValueError, TypeError) as exc:
            raise UserError(f"cannot read when lessons were last seen: {shown(str(exc))}") from None
    if args.firing or args.stale:
        counts = _shown_counts(since)
        if args.firing:
            lessons = [le for le in lessons if counts.get(le.id)]
        if args.stale:
            lessons = [
                le for le in lessons if not counts.get(le.id) and _seen_before(last_seen.get(le.id), since)
            ]
    if not lessons and not problems:
        ctx.print("no lessons")
        return 0
    if lessons:
        ctx.print(f"{'ID':<11} {'STATUS':<11} {'LEVEL':<4} PIN {'SEEN':>4}  SCOPE  AVOID")
    for lesson in lessons:
        ctx.print(row(lesson))
    shown_ids = {le.id for le in lessons}
    for lesson_id, exc in refused:
        if lesson_id in shown_ids:
            ctx.print(f"cannot render: lesson {shown(lesson_id)} breaks {exc.rule} in {exc.field}")
    for lesson_id in to_review:
        if lesson_id in shown_ids:
            ctx.print(
                f"needs review: lesson {shown(lesson_id)} has an error trigger made by older signature rules"
            )
    for lesson_id, reason in problems:
        ctx.print(f"problem: lesson {shown(lesson_id)} cannot be read: {shown(reason)}")
    return 1 if problems else 0


def _seen_before(at: object, since: float) -> bool:
    return not isinstance(at, int | float) or at < since


def describe(lesson: Lesson) -> list[str]:
    pinned = "pinned" if lesson.pinned else "not pinned"
    sessions = "1 session" if lesson.sessions_seen == 1 else f"{lesson.sessions_seen} sessions"
    lines = [
        f"{shown(lesson.id)}  {lesson.status}  level {lesson.level}  {pinned}  "
        f"{shown(render.scope_label(lesson.scope))}  trust {lesson.trust_class}  seen in {sessions}",
        f"  avoid:   {shown(lesson.text.avoid)}",
        f"  instead: {shown(lesson.text.instead)}",
        f"  reason:  {shown(lesson.text.reason)}",
    ]
    return lines


def _hit_lines(db: sqlite3.Connection, hit: telemetry.Hit) -> list[str]:
    lines = [
        f"last fired: {when(hit.at)} {harness_name(str(hit.harness))} {shown(str(hit.event))} "
        f"in session {shown(str(hit.session_id))}",
        f"  matched trigger: {shown(hit.trigger_key) if hit.trigger_key else 'none recorded'}",
    ]
    call = store.mined_call(db, hit.tool_use_id) if hit.tool_use_id else None
    if call is not None and call.command is not None:
        lines.append(f"  matched input: {shown(call.command)}")
    elif call is not None and call.paths:
        lines.append(f"  matched input: {shown(', '.join(call.paths))}")
    else:
        lines.append("  matched input: not recorded yet (scars mine reads it from the transcript)")
    return lines


def _why(db: sqlite3.Connection, ctx: Context, lesson: Lesson, hit: telemetry.Hit | str | None) -> None:
    """`hit` is the last shown hit, None when there is none, or why it could not be read."""
    for line in describe(lesson):
        ctx.print(line)
    ctx.print("triggers:")
    for t in lesson.triggers:
        ctx.print(f"  {t.kind} at {t.stage}: {shown(t.spec)}")
    if not lesson.triggers:
        ctx.print("  none (a pinned lesson fires at session start without one)")
    if hit is None:
        ctx.print("last fired: never")
    elif isinstance(hit, str):
        ctx.print(f"last fired: cannot read the hit: {hit}")
    else:
        try:
            lines = _hit_lines(db, hit)
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            lines = [f"last fired: cannot read the hit: {shown(str(exc))}"]
        for line in lines:
            ctx.print(line)
    observations = store.observations(db, lesson.id)
    ctx.print(f"observations: {len(observations)}")
    for o in observations:
        ctx.print(f"  {when(o.at)} {harness_name(o.harness)} session {shown(o.session_id)}")
        ctx.print(f"    failed: {shown(o.failed_input)}")
        ctx.print(f"    error:  {shown(o.error_signature)}")
        ctx.print(f"    passed: {shown(o.passing_input)}")
    ctx.print("events:")
    for e in store.lesson_events(db, lesson.id):
        rev = f"{e.from_revision or '-'} -> {e.to_revision}"
        payload = "" if e.payload == "{}" or e.type == "proposed" else f" {shown_json(str(e.payload))}"
        ctx.print(f"  {when(e.at)} {shown(str(e.type))} by {shown(str(e.actor))} (revision {rev}){payload}")


def cmd_why(args: argparse.Namespace, ctx: Context) -> int:
    tele = telemetry_db()
    hit: telemetry.Hit | str | None
    try:
        if args.id == "last":
            try:
                hit = telemetry.last_shown(tele) if tele else None
            except STORED_DATA_ERRORS as exc:
                raise UserError(f"cannot read telemetry.db: {shown(str(exc))}") from None
            if hit is None or not isinstance(hit.lesson_id, str):
                raise UserError("no lesson has been shown yet")
            lesson_id = hit.lesson_id
        else:
            lesson_id = args.id
            try:
                hit = telemetry.last_shown(tele, LessonId(lesson_id)) if tele else None
            except STORED_DATA_ERRORS as exc:
                hit = shown(str(exc))
    finally:
        if tele is not None:
            tele.close()
    with lessons_db(readonly=True) as db:
        lesson = get_lesson(db, lesson_id)
        try:
            _why(db, ctx, lesson, hit)
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            raise UserError(f"cannot read the store: {shown(str(exc))}") from None
    return 0


def _root_of() -> object:
    cache: dict[str, str | None] = {}

    def root(cwd: str) -> str | None:
        if cwd not in cache:
            found = known_dir(cwd)
            cache[cwd] = hook.repo_root(found) if found else None
        return cache[cwd]

    return root


def run_precision(
    db: sqlite3.Connection, lesson: Lesson, now: float
) -> tuple[int, list[tuple[str, Precision | None]]]:
    """Replay each command or path pre-trigger over the last 30 days of mined calls.

    Returns the number of calls and, per pre-trigger, its label and result. The result is None when the
    stored spec does not parse.
    """
    errors = [t for t in lesson.triggers if t.kind is TriggerKind.ERROR]
    pre = [t for t in lesson.triggers if t.kind in (TriggerKind.COMMAND, TriggerKind.PATH)]
    if not errors or not pre:
        return 0, []
    signature = ErrorSignature(errors[0].spec)
    calls = store.mined_calls_since(db, now - WINDOW_DAYS * DAY)
    root = _root_of()
    out: list[tuple[str, Precision | None]] = []
    for trigger in pre:
        replay = [recorded(c, root(c.cwd) if trigger.kind is TriggerKind.PATH else None) for c in calls]  # type: ignore[operator]
        try:
            result: Precision | None = precision(trigger, replay, error_signature=signature)
        except InvalidTrigger:
            result = None
        out.append((f"{trigger.kind} at {trigger.stage}: {shown(trigger.spec)}", result))
    return len(calls), out


def cmd_test(args: argparse.Namespace, ctx: Context) -> int:
    now = ctx.clock()
    with lessons_db() as db:
        lesson = get_lesson(db, args.id)
        try:
            calls, results = run_precision(db, lesson, now)
        except (ValueError, TypeError) as exc:
            raise UserError(f"cannot replay {shown(lesson.id)}: {shown(str(exc))}") from None
        if not results:
            ctx.print(
                f"{shown(lesson.id)} has no command or path trigger with an error trigger; nothing to test"
            )
            return 0
        ctx.print(f"{shown(lesson.id)}: replayed {calls} mined calls from the last {WINDOW_DAYS} days")
        replayed = [(label, p) for label, p in results if p is not None]
        for label, p in results:
            if p is None:
                ctx.print(f"  {label}")
                ctx.print("    cannot replay: not a valid trigger spec")
                continue
            keep = "yes" if p.keeps_pre_trigger else "no"
            block = "yes" if p.allows_block else "no"
            ctx.print(f"  {label}")
            ctx.print(
                f"    matched_failing {p.matched_failing}  matched_passing {p.matched_passing}  "
                f"matched {p.matched}"
            )
            ctx.print(f"    keep rule (at most 4 passing per failing): {keep}")
            ctx.print(f"    block rule (at least 3 failing, under 1% passing): {block}")
        if len(replayed) < len(results):
            return 1
        worst = min(replayed, key=lambda r: (r[1].allows_block, r[1].keeps_pre_trigger))[1]
        store.record_precision(
            db,
            lesson.id,
            lesson.revision,
            matched_failing=worst.matched_failing,
            matched_passing=worst.matched_passing,
            window_days=WINDOW_DAYS,
            at=now,
        )
    return 0
