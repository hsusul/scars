"""SQLite persistence for lessons.db and telemetry.db. See docs/adr/0002-sqlite-store.md."""

import sqlite3
from importlib.resources import files
from pathlib import Path

from scars.model import EventType, LessonId


def schema_sql(name: str) -> str:
    """The DDL for `lessons` or `telemetry`, shipped in `scars/schema/`."""
    return files("scars.schema").joinpath(f"{name}.sql").read_text()


def connect(path: Path, *, readonly: bool = False, busy_timeout_ms: int = 2000) -> sqlite3.Connection:
    raise NotImplementedError("B1 store")


def init_lessons(path: Path) -> sqlite3.Connection:
    raise NotImplementedError("B1 store")


def init_telemetry(path: Path) -> sqlite3.Connection:
    raise NotImplementedError("B1 store")


class Conflict(Exception):
    """A compare-and-set update found a newer revision than the caller expected."""


class Rejected(Exception):
    """The lifecycle table forbids this event for the lesson's current status."""


def apply_event(
    db: sqlite3.Connection,
    *,
    lesson_id: LessonId,
    event: EventType,
    expected_revision: int,
    actor: str,
    payload: dict[str, object],
) -> int:
    """Apply one lifecycle event in one transaction and return the new revision."""
    raise NotImplementedError("B1 store")
