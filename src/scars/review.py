"""Lessons waiting for a person's review.

The session-start notice, the `scars mine` summary and `scars inbox` all count with `waiting`, so the
three numbers agree. A lesson waits only when the inbox can show it and approve it. Text that no longer
renders and session-scoped lessons, which name no registered repo to promote into, are counted apart as
hidden, so the inbox can say what it left out.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from scars import render, store
from scars.model import Lesson, SessionScope, Status


@dataclass(frozen=True)
class Waiting:
    lessons: tuple[Lesson, ...]
    unrenderable: int
    unregistered: int


def waiting(db: sqlite3.Connection) -> Waiting:
    """Candidate and provisional lessons the inbox can show and approve, and how many it hides."""
    pending = store.list_lessons(db, statuses=(Status.CANDIDATE, Status.PROVISIONAL))
    known = store.passing_inputs(db, [le.id for le in pending])
    repos = store.repo_names(db)
    shown: list[Lesson] = []
    unrenderable = unregistered = 0
    for lesson in pending:
        if render.render_problem(lesson, known_commands=known[lesson.id], registered_repos=repos) is not None:
            unrenderable += 1
        elif isinstance(lesson.scope, SessionScope):
            unregistered += 1
        else:
            shown.append(lesson)
    return Waiting(tuple(shown), unrenderable, unregistered)


def hidden_line(found: Waiting) -> str | None:
    """One line saying what the inbox hides and why, or None when it hides nothing."""
    parts = []
    if found.unrenderable:
        parts.append(f"{found.unrenderable} that cannot render (scars doctor names the rule)")
    if found.unregistered:
        parts.append(
            f"{found.unregistered} from a directory that is not a registered repo "
            "(scars init offers to register the repos your sessions used)"
        )
    return "hidden: " + "; ".join(parts) if parts else None
