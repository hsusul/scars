"""`scars inbox`: review waiting candidate and provisional lessons, one keystroke each.

At most 5 are shown, best first by `sessions_seen * consistency * 0.5 ** (days_since_last_seen / 14)`,
where `consistency` is the share of observations whose passing command used the most common program.
Keys: `a` approves (promotes in the lesson's own scope), `e` opens the three text fields in `$EDITOR`,
`m` mutes, `s` skips and `q` quits. Like the privileged commands, the inbox refuses to apply a decision
unless a person runs it at a terminal.

An edited lesson's text is the human's, so its trust class becomes `human` and the text is checked with
the rules for human text. Because `human` lifts the observed-command rule, an edit is never applied on
the editor's word alone (the editor could be a script that rewrites mined text): the inbox prints the
lesson exactly as agents will see it and applies the edit only after the person presses `y`. When the
new `instead` names a backticked command that never passed on this machine, it says so and needs a
second `y`. Any other key cancels. The edit sends the lesson back to candidate, and the inbox shows it
again so the person can approve the new wording at once. An edit that would not fit a stage budget once
its header reaches its longest form is refused before the preview, so it never stops rendering later.
"""

from __future__ import annotations

import argparse
import shlex
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum

from scars import render, review, store
from scars.commands import Context, UserError, lessons_db, rejection, require_human, shown
from scars.commands.inspect import describe
from scars.commands.lifecycle import apply, promote
from scars.model import EventType, Lesson, Status, TrustClass

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
    """Every waiting lesson (see `review.waiting`) with its inbox score, best first, ties by id."""
    out = []
    for lesson in review.waiting(db).lessons:
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


class Edit(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    CANCELLED = "cancelled"


def unobserved_command(instead: str, known_commands: frozenset[str]) -> str | None:
    """The backticked command in a checked `instead` when no observed passing input equals it."""
    if instead.count("`") != 2:
        return None
    command = instead.split("`")[1]
    known = {render.normalize_command(k) for k in known_commands}
    return None if render.normalize_command(command) in known else command


def edit(db: sqlite3.Connection, ctx: Context, lesson: Lesson) -> Edit:
    """Run the editor, show the result as agents will see it, and apply it after the person's `y`."""
    fields = parse_edit(ctx.rewrite(edit_text(lesson)))
    if all(fields[n] == getattr(lesson.text, n) for n in FIELDS):
        return Edit.UNCHANGED
    known = store.passing_inputs(db, [lesson.id])[lesson.id]
    repos = store.repo_names(db)
    try:
        text = render.check_fields(
            fields["avoid"],
            fields["instead"],
            fields["reason"],
            lesson.scope,
            known_commands=known,
            trust_class=TrustClass.HUMAN,
            registered_repos=repos,
        )
        after = replace(lesson, text=text, trust_class=TrustClass.HUMAN, status=Status.CANDIDATE)
        render.check_budgets(after, known_commands=known, registered_repos=repos)
        shown = render.render(after, known_commands=known, registered_repos=repos)
    except render.RejectedText as exc:
        raise UserError(rejection(exc)) from None
    ctx.print("this is how agents will see the edited lesson:")
    ctx.print(shown)
    ctx.print("press y to save it as your own text (trust human); any other key cancels")
    if ctx.key() != "y":
        return Edit.CANCELLED
    if (command := unobserved_command(text.instead, known)) is not None:
        ctx.print(
            f"`{command}` never ran successfully on this machine; "
            "press y again to keep it, any other key cancels"
        )
        if ctx.key() != "y":
            return Edit.CANCELLED
    payload: dict[str, object] = {
        "text": {"avoid": text.avoid, "instead": text.instead, "reason": text.reason},
        "trust_class": TrustClass.HUMAN.value,
    }
    apply(db, ctx, lesson, EventType.EDITED, payload)
    return Edit.APPLIED


def _source(db: sqlite3.Connection, lesson: Lesson) -> list[str]:
    found = store.observations(db, lesson.id)
    if not found:
        return []
    o = found[-1]
    return [
        "  source evidence, as recorded (agents never see it):",
        f"    failed: {shown(o.failed_input)}",
        f"    error:  {shown(o.error_signature)}",
        f"    passed: {shown(o.passing_input)}",
    ]


def cmd_inbox(_: argparse.Namespace, ctx: Context) -> int:
    counts: Counter[str] = Counter()
    with lessons_db() as db:
        if (hidden := review.hidden_line(review.waiting(db))) is not None:
            ctx.print(hidden)
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
                    promote(db, ctx, lesson, lesson.scope)
                    counts["approved"] += 1
                elif key is Key.MUTE:
                    apply(db, ctx, lesson, EventType.MUTED, {})
                    ctx.print(f"muted {lesson.id}")
                    counts["muted"] += 1
                elif key is Key.EDIT:
                    match edit(db, ctx, lesson):
                        case Edit.APPLIED:
                            ctx.print(f"edited {lesson.id}; it is a candidate again")
                            counts["edited"] += 1
                        case Edit.UNCHANGED:
                            ctx.print("no change")
                        case Edit.CANCELLED:
                            ctx.print("edit cancelled; nothing changed")
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
