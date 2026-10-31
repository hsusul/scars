"""Provisional, expiry and precision rules for mined lessons.

- A mined candidate becomes provisional on its first sighting when its trust class is `own_code`, its
  scope is a repo, and the command it suggests is not destructive by `DESTRUCTIVE`. Lessons from
  upstream repos (`third_party_repo`) or the web never become provisional on their own; a human promotes
  them or they expire. A command that worked once is no reason to suggest deleting files or rewriting
  history to other sessions unreviewed, so a destructive fix waits for a human too. The table fails
  closed: a command the shell parser rejects counts as destructive. Only the miner's own first sighting
  provisionalizes: a candidate that exists for another reason (a human edit sent it back) is left alone.
- A command or path pre-trigger ships only when the precision check over the last 30 days of mined
  calls keeps it: at most 4 passing matches per failing match. Otherwise the lesson is stored with its
  error trigger alone. The decision is made before the lesson is first stored, because changing a stored
  lesson's triggers is an edit, which a human makes.
- Candidate and provisional lessons expire 14 days after their last sighting.
"""

import os
import shlex
import sqlite3
from dataclasses import dataclass

from scars import store
from scars.match import Precision
from scars.model import EventType, RepoScope, Scope, TrustClass
from scars.shell import SimpleCommand, Unsupported, parse

PRECISION_WINDOW_DAYS = 30
EXPIRY_SECONDS = store.EXPIRY_SECONDS
ACTOR = "miner"


@dataclass(frozen=True)
class Destructive:
    """A program (or every `program.*`, as `mkfs.ext4`), optionally only with a subcommand, and only with
    an argument starting with one of `prefixes` or a short-option cluster holding one of `letters`.
    With neither, every run of it counts."""

    program: str
    subcommand: str | None = None
    prefixes: tuple[str, ...] = ()
    letters: str = ""

    def hits(self, command: SimpleCommand) -> bool:
        name = os.path.basename(command.program)
        if name != self.program and not name.startswith(self.program + "."):
            return False
        if self.subcommand is not None and self.subcommand not in command.args:
            return False
        if not self.prefixes and not self.letters:
            return True
        return any(
            arg.startswith(self.prefixes)
            or (arg[:1] == "-" and arg[1:2] != "-" and any(ch in self.letters for ch in arg[1:]))
            for arg in command.args
        )


DESTRUCTIVE = (
    Destructive("rm"),
    Destructive("dd"),
    Destructive("mkfs"),
    Destructive("truncate"),
    Destructive("git", "reset", ("--hard",)),
    Destructive("git", "clean"),
    Destructive("git", "checkout", ("--force",), "f"),
    Destructive("git", "push", ("--force", "+"), "f"),
    Destructive("find", None, ("-delete", "-exec")),
    Destructive("chmod", None, ("--recursive",), "R"),
    Destructive("chown", None, ("--recursive",), "R"),
)


ESCALATION = frozenset({"sudo", "doas"})
"""Checked on the raw words: the parser unwraps them and reports the program they run."""


def destructive(command: str) -> bool:
    try:
        parsed = parse(command)
        words = shlex.split(command)
    except (Unsupported, ValueError):
        return True
    if any(os.path.basename(w) in ESCALATION for w in words):
        return True
    return not parsed or any(rule.hits(c) for c in parsed for rule in DESTRUCTIVE)


def provisional_on_first_sighting(scope: Scope, trust_class: TrustClass, fix_command: str) -> bool:
    """`fix_command` is the command the lesson's `instead` suggests."""
    return (
        isinstance(scope, RepoScope) and trust_class is TrustClass.OWN_CODE and not destructive(fix_command)
    )


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
