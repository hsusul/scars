"""`scars import-guard`: turn a standalone guard hook's deny reasons into pinned plan-stage lessons.

The guard keeps enforcing its rules on its own; the lessons only teach them at session start, before
the attempt. So every imported lesson is promoted, pinned, machine-scoped and has no trigger at all:
never a gate lesson that could deny a call, which would make a safety control depend on this store.

A deny reason is a quoted string right after the word `deny`, as in `decide deny "..."`. The reason's
first clause (up to `:` or the first sentence end) becomes `avoid` and the rest `instead`; `reason`
names the guard. Each lesson's fingerprint comes from the deny text, so importing twice adds nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from scars import render, store
from scars.commands import Context, UserError, lessons_db, require_human
from scars.model import (
    EventType,
    LessonId,
    LessonText,
    MachineScope,
    SessionId,
    SessionScope,
    Trigger,
    TrustClass,
)

ACTOR = "human"
DEFAULT_GUARD = Path(".claude") / "hooks" / "guard-bash.sh"
_DENY = re.compile(r"""\bdeny\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")
_SPLIT = re.compile(r":\s+|(?<=[.;])\s+")
STAGING_SESSION = SessionId("scars-import")
"""A candidate cannot start in machine scope, so a lesson is created in this unused session scope and
promoted to machine scope in the same transaction."""


def deny_reasons(script: str) -> list[str]:
    out = []
    for m in _DENY.finditer(script):
        text = m[1] if m[1] is not None else m[2]
        text = re.sub(r"\\(.)", r"\1", text).strip()
        if text and text not in out:
            out.append(text)
    return out


@dataclass(frozen=True)
class Draft:
    deny: str
    avoid: str
    instead: str
    reason: str


def draft(deny: str, guard_name: str) -> Draft:
    parts = _SPLIT.split(deny, maxsplit=1)
    avoid = parts[0].rstrip(".;")
    instead = parts[1] if len(parts) > 1 and parts[1] else "follow the guard's rule"
    return Draft(deny, avoid, instead, f"the {guard_name} guard denies this, and it keeps enforcing it")


def create_pinned(
    db: sqlite3.Connection,
    text: LessonText,
    triggers: tuple[Trigger, ...],
    fingerprint: str,
    *,
    actor: str,
    at: float,
) -> LessonId:
    """Create a promoted, pinned, machine-scoped lesson in one transaction."""
    with store.transaction(db):
        lesson_id = store.create_lesson(
            db,
            text=text,
            scope=SessionScope(STAGING_SESSION),
            trust_class=TrustClass.HUMAN,
            triggers=triggers,
            fingerprint=fingerprint,
            actor=actor,
            at=at,
        )
        revision = store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PROMOTED,
            expected_revision=1,
            actor=actor,
            payload={"scope": store.scope_to_json(MachineScope())},
            at=at,
        )
        store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PINNED,
            expected_revision=revision,
            actor=actor,
            payload={},
            at=at,
        )
    return lesson_id


def fingerprint(deny: str) -> str:
    return "guard:" + hashlib.sha256(deny.encode()).hexdigest()


def cmd_import_guard(args: argparse.Namespace, ctx: Context) -> int:
    require_human(ctx)
    path = Path(args.path) if args.path else ctx.user_home / DEFAULT_GUARD
    try:
        script = path.read_text()
    except OSError as exc:
        raise UserError(f"cannot read {path}: {exc.strerror}") from None
    reasons = deny_reasons(script)
    if not reasons:
        raise UserError(f"no deny reasons found in {path}")
    imported = 0
    with lessons_db() as db:
        repos = store.repo_names(db)
        for deny in reasons:
            d = draft(deny, path.name)
            if store.get_lesson_by_fingerprint(db, fingerprint(deny)) is not None:
                ctx.print(f"already imported: {deny[:60]}")
                continue
            try:
                text = render.check_fields(
                    d.avoid,
                    d.instead,
                    d.reason,
                    MachineScope(),
                    known_commands=frozenset(),
                    trust_class=TrustClass.HUMAN,
                    registered_repos=repos,
                )
            except render.RejectedText as exc:
                ctx.print(f"skipped ({exc.field} breaks {exc.rule}): {deny[:60]}")
                continue
            lesson_id = create_pinned(db, text, (), fingerprint(deny), actor=ACTOR, at=ctx.clock())
            ctx.print(f"imported {lesson_id}: AVOID {text.avoid}")
            imported += 1
    ctx.print(f"{imported} pinned plan-stage lesson{'s' if imported != 1 else ''} from {path}")
    return 0
