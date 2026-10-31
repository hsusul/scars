"""Expiry and precision rules for mined lessons.

- A mined lesson stays a candidate in the inbox until a human approves it; the miner never shares one
  with an agent on its own, not even with the session it was mined from.
- A command or path pre-trigger ships only when the precision check over the last 30 days of mined
  calls keeps it: at most 4 passing matches per failing match. Otherwise the lesson is stored with its
  error trigger alone. The decision is made before the lesson is first stored, because changing a stored
  lesson's triggers is an edit, which a human makes.
- Candidate and provisional lessons expire 14 days after their last sighting.
"""

import sqlite3

from scars import store
from scars.match import Precision
from scars.model import EventType

PRECISION_WINDOW_DAYS = 30
EXPIRY_SECONDS = store.EXPIRY_SECONDS
ACTOR = "miner"


def keeps_pre_trigger(result: Precision) -> bool:
    return result.keeps_pre_trigger


def expire(db: sqlite3.Connection, now: float) -> int:
    """Retire every candidate and provisional lesson not sighted for 14 days. Returns how many."""
    expired = 0
    for lesson_id, revision in store.expiring_lessons(db, now):
        try:
            store.apply_event(
                db,
                lesson_id=lesson_id,
                event=EventType.EXPIRED,
                expected_revision=revision,
                actor=ACTOR,
                payload={},
                at=now,
            )
        except (store.Conflict, store.Rejected):
            continue
        expired += 1
    return expired
