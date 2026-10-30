"""`scars inbox`: review waiting candidate and provisional lessons, one keystroke each.

At most 5 are shown, best first by `sessions_seen * consistency * 0.5 ** (days_since_last_seen / 14)`,
where `consistency` is the share of observations whose passing command used the most common program.
Keys: `a` approves (promotes in the lesson's own scope), `e` opens the three text fields in `$EDITOR`,
`m` mutes, `s` skips and `q` quits. Like the privileged commands, the inbox refuses to apply a decision
unless a person runs it at a terminal.

An edited lesson's text is the human's, so its trust class becomes `human` and the text is checked with
the rules for human text. The edit sends the lesson back to candidate, and the inbox shows it again so
the person can approve the new wording at once.
"""

from __future__ import annotations

import argparse
import shlex
import sqlite3
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from scars import render, store
from scars.commands import Context, UserError, lessons_db, require_human
from scars.commands.inspect import describe
from scars.commands.lifecycle import apply, promote
from scars.model import EventType, Lesson, SessionScope, Status, TrustClass

PAGE = 5
HALF_LIFE_DAYS = 14
DAY = 86400.0
FIELDS = ("avoid", "instead", "reason")
EDIT_HELP = "# Rewrite the lesson. Lines starting with # are ignored. Keep each field on one line.\n"


class Key(StrEnum):
    APPROVE = "a"
    EDIT = "e"
    MUTE = "m"
    SKIP = "s"
    QUIT = "q"


PROMPT = "a approve  e edit  m mute  s skip  q quit"


def program(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    return words[0] if words else ""


def consistency(passing_inputs: list[str]) -> float:
    if not passing_inputs:
        return 1.0
    counts = Counter(program(p) for p in passing_inputs)
    return counts.most_common(1)[0][1] / len(passing_inputs)


@dataclass(frozen=True)
class Ranked:
    lesson: Lesson
    score: float


def rank(db: sqlite3.Connection, now: float) -> list[Ranked]:
    """Every candidate and provisional lesson with its inbox score, best first, ties by id."""
    out = []
    for lesson in store.list_lessons(db, statuses=(Status.CANDIDATE, Status.PROVISIONAL)):
        seen = store.lesson_last_seen(db, lesson.id) or now
        age_days = max(0.0, now - seen) / DAY
        passing = [o.passing_input for o in store.observations(db, lesson.id)]
        score = lesson.sessions_seen * consistency(passing) * 0.5 ** (age_days / HALF_LIFE_DAYS)
        out.append(Ranked(lesson, score))
    return sorted(out, key=lambda r: (-r.score, r.lesson.id))


def edit_text(lesson: Lesson) -> str:
    return EDIT_HELP + "".join(f"{name}: {getattr(lesson.text, name)}\n" for name in FIELDS)


def parse_edit(text: str) -> dict[str, str]:
    """The three fields from an edited file, or `UserError` naming what is wrong."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition(":")
        name = name.strip()
        if not sep or name not in FIELDS:
            raise UserError(f"unexpected line {line[:40]!r}; each line is avoid:, instead: or reason:")
        if name in found:
            raise UserError(f"{name} appears twice")
        found[name] = value.strip()
    missing = [n for n in FIELDS if n not in found]
    if missing:
        raise UserError(f"missing {', '.join(missing)}")
    return found


def edit(db: sqlite3.Connection, ctx: Context, lesson: Lesson) -> bool:
    """Run the editor and apply the edit. False when nothing changed."""
    fields = parse_edit(ctx.rewrite(edit_text(lesson)))
    if all(fields[n] == getattr(lesson.text, n) for n in FIELDS):
        return False
    try:
        text = render.check_fields(
            fields["avoid"],
            fields["instead"],
            fields["reason"],
            lesson.scope,
            known_commands=store.passing_inputs(db, [lesson.id])[lesson.id],
            trust_class=TrustClass.HUMAN,
            registered_repos=store.repo_names(db),
        )
    except render.RejectedText as exc:
        raise UserError(f"{exc.field} breaks {exc.rule}") from None
    payload: dict[str, object] = {
        "text": {"avoid": text.avoid, "instead": text.instead, "reason": text.reason},
        "trust_class": TrustClass.HUMAN.value,
    }
    apply(db, ctx, lesson, EventType.EDITED, payload)
    return True


def _source(db: sqlite3.Connection, lesson: Lesson) -> list[str]:
    found = store.observations(db, lesson.id)
    if not found:
        return []
    o = found[-1]
    return [f"  source:  `{o.failed_input}` failed, then `{o.passing_input}` passed"]


def cmd_inbox(_: argparse.Namespace, ctx: Context) -> int:
    counts: Counter[str] = Counter()
    with lessons_db() as db:
        queue = rank(db, ctx.clock())
        page = queue[:PAGE]
        if not page:
            ctx.print("inbox is empty")
            return 0
        position = 0
        while position < len(page):
            lesson = store.get_lesson(db, page[position].lesson.id)
            assert lesson is not None
            ctx.print("")
            ctx.print(f"[{position + 1}/{len(page)}] score {page[position].score:.2f}")
            for line in (*describe(lesson), *_source(db, lesson)):
                ctx.print(line)
            ctx.print(PROMPT)
            pressed = ctx.key()
            if pressed in ("", Key.QUIT):
                break
            if pressed not in set(Key):
                ctx.print(f"unknown key {pressed!r}")
                continue
            key = Key(pressed)
            if key is Key.SKIP:
                counts["skipped"] += 1
                position += 1
                continue
            require_human(ctx)
            try:
                if key is Key.APPROVE:
                    if isinstance(lesson.scope, SessionScope):
                        raise UserError(
                            f"{lesson.id} is session-scoped; run scars promote {lesson.id} --repo <repo-id>"
                        )
                    promote(db, ctx, lesson, lesson.scope)
                    counts["approved"] += 1
                elif key is Key.MUTE:
                    apply(db, ctx, lesson, EventType.MUTED, {})
                    ctx.print(f"muted {lesson.id}")
                    counts["muted"] += 1
                elif key is Key.EDIT:
                    if edit(db, ctx, lesson):
                        ctx.print(f"edited {lesson.id}; it is a candidate again")
                        counts["edited"] += 1
                    else:
                        ctx.print("no change")
                    continue
            except UserError as exc:
                ctx.print(f"not applied: {exc}")
                if key is Key.EDIT:
                    continue
            position += 1
        waiting = len(rank(db, ctx.clock()))
    summary = ", ".join(f"{counts[k]} {k}" for k in ("approved", "edited", "muted", "skipped"))
    ctx.print(f"inbox: {summary}; {waiting} waiting")
    return 0
