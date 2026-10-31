"""The privileged commands that change a lesson's reach, pin, level or status, and the kill switch.

Every one refuses unless a person runs it at a terminal (see `refusal`). Lesson changes go through
`store.apply_event` with the revision the command read, so a change made in between is reported as a
conflict instead of being overwritten. Level `block` does not exist yet, so `level` takes `note` or
`warn` only.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import replace

from scars import paths, render, store
from scars.commands import Context, UserError, get_lesson, lessons_db, rejection, require_human
from scars.model import EventType, Lesson, Level, MachineScope, RepoId, RepoScope, Scope, SessionScope

ACTOR = "human"
SETTABLE_LEVELS = (Level.NOTE.value, Level.WARN.value)


def apply(
    db: sqlite3.Connection, ctx: Context, lesson: Lesson, event: EventType, payload: dict[str, object]
) -> int:
    """Apply one human event at the revision the caller read. Conflicts and rejections become user errors."""
    try:
        return store.apply_event(
            db,
            lesson_id=lesson.id,
            event=event,
            expected_revision=lesson.revision,
            actor=ACTOR,
            payload=payload,
            at=ctx.clock(),
        )
    except store.Conflict as exc:
        raise UserError(f"conflict: {exc}; it changed since it was read, so run the command again") from None
    except store.Rejected as exc:
        raise UserError(f"{lesson.id}: {exc}") from None


def promotion_scope(db: sqlite3.Connection, lesson: Lesson, *, machine: bool, repo: str | None) -> Scope:
    """The scope a promotion gives the lesson: its own unless `--machine` or `--repo` says otherwise.

    A session-scoped lesson has no reach of its own, so it needs one of the two. Machine scope is checked
    against the machine-scope text rules first, because the hook would otherwise refuse to render it.
    """
    if machine:
        try:
            render.check(
                lesson.text,
                MachineScope(),
                known_commands=store.passing_inputs(db, [lesson.id])[lesson.id],
                trust_class=lesson.trust_class,
                registered_repos=store.repo_names(db),
                writing=True,
            )
        except render.RejectedText as exc:
            raise UserError(f"{lesson.id} cannot have machine scope: {rejection(exc)}") from None
        return MachineScope()
    if repo is not None:
        if store.repo_owner_class(db, RepoId(repo)) is None:
            raise UserError(f"no registered repo {repo}")
        return RepoScope(RepoId(repo))
    if isinstance(lesson.scope, SessionScope):
        raise UserError(f"{lesson.id} is session-scoped; pass --repo <repo-id> or --machine")
    return lesson.scope


def promote(db: sqlite3.Connection, ctx: Context, lesson: Lesson, scope: Scope) -> None:
    """Promote `lesson` into `scope`, unless the lesson there would not fit a stage budget: a longer
    scope label lengthens the header, and promotion adds the review date."""
    try:
        render.check_budgets(
            replace(lesson, scope=scope),
            known_commands=store.passing_inputs(db, [lesson.id])[lesson.id],
            registered_repos=store.repo_names(db),
        )
    except render.RejectedText as exc:
        label = render.scope_label(scope)
        raise UserError(f"{lesson.id} cannot be promoted to {label}: {rejection(exc)}") from None
    payload: dict[str, object] = {} if scope == lesson.scope else {"scope": store.scope_to_json(scope)}
    apply(db, ctx, lesson, EventType.PROMOTED, payload)
    ctx.print(f"promoted {lesson.id} ({render.scope_label(scope)}, level note, not pinned)")


def cmd_promote(args: argparse.Namespace, ctx: Context) -> int:
    require_human(ctx)
    with lessons_db() as db:
        lesson = get_lesson(db, args.id)
        promote(db, ctx, lesson, promotion_scope(db, lesson, machine=args.machine, repo=args.repo))
    return 0


def _simple(event: EventType, done: str) -> object:
    def handler(args: argparse.Namespace, ctx: Context) -> int:
        require_human(ctx)
        with lessons_db() as db:
            lesson = get_lesson(db, args.id)
            apply(db, ctx, lesson, event, {})
        ctx.print(f"{done} {lesson.id}")
        return 0

    return handler


cmd_pin = _simple(EventType.PINNED, "pinned")
cmd_unpin = _simple(EventType.UNPINNED, "unpinned")
cmd_retire = _simple(EventType.RETIRED, "retired")
cmd_mute = _simple(EventType.MUTED, "muted")


def cmd_level(args: argparse.Namespace, ctx: Context) -> int:
    require_human(ctx)
    if args.level == Level.BLOCK.value:
        raise UserError("level block is not available in this version; use note or warn")
    if args.level not in SETTABLE_LEVELS:
        raise UserError(f"unknown level {args.level!r}; use note or warn")
    with lessons_db() as db:
        lesson = get_lesson(db, args.id)
        apply(db, ctx, lesson, EventType.LEVEL_SET, {"level": args.level})
    ctx.print(f"{lesson.id} level {args.level}")
    return 0


def cmd_off(_: argparse.Namespace, ctx: Context) -> int:
    require_human(ctx)
    switch = paths.kill_switch()
    switch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    switch.touch()
    ctx.print(
        f"scars is off: every hook does nothing from its next call ({switch} exists). scars on undoes it."
    )
    return 0


def cmd_on(_: argparse.Namespace, ctx: Context) -> int:
    require_human(ctx)
    switch = paths.kill_switch()
    if switch.exists():
        switch.unlink()
        ctx.print("scars is on: hooks consult lessons again from their next call")
    else:
        ctx.print("scars is already on")
    if paths.off_value(ctx.env.get("SCARS_OFF")):
        ctx.print("SCARS_OFF is set in this environment, which keeps hooks started from it off")
    return 0
