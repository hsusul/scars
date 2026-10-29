"""Trigger evaluation under a deadline. See ARCHITECTURE "Matching"."""

from scars.model import HookEvent, Lesson, Trigger


def matches(trigger: Trigger, event: HookEvent) -> bool:
    raise NotImplementedError("B2 match")


def select(lessons: tuple[Lesson, ...], event: HookEvent, *, deadline_ms: int = 50) -> tuple[Lesson, ...]:
    raise NotImplementedError("B2 match")
