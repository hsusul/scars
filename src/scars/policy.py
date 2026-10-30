"""Provisional, expiry and precision rules for mined lessons.

- A mined candidate becomes provisional on its first sighting when its trust class is `own_code` and
  its scope is a repo. Lessons from upstream repos (`third_party_repo`) or the web never become
  provisional on their own; a human promotes them or they expire. Only the miner's own first sighting
  provisionalizes: a candidate that exists for another reason (a human edit sent it back) is left alone.
- A command or path pre-trigger ships only when the precision check over the last 30 days of mined
  calls keeps it: at most 4 passing matches per failing match. Otherwise the lesson is stored with its
  error trigger alone. The decision is made before the lesson is first stored, because changing a stored
  lesson's triggers is an edit, which a human makes.
- Candidate and provisional lessons expire 14 days after their last sighting.
"""

import sqlite3

from scars import store
from scars.match import Precision
from scars.model import EventType, RepoScope, Scope, TrustClass

PRECISION_WINDOW_DAYS = 30
EXPIRY_SECONDS = store.EXPIRY_SECONDS
ACTOR = "miner"


def provisional_on_first_sighting(scope: Scope, trust_class: TrustClass) -> bool:
    return isinstance(scope, RepoScope) and trust_class is TrustClass.OWN_CODE


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
