"""Lesson template, sanitizer, lints and secret scan."""

from scars.model import Lesson, LessonText, Scope


class RejectedText(Exception):
    """Text failed the sanitizer, a lint, or the secret scan, and must not be stored or shown."""


def check(text: LessonText, scope: Scope) -> None:
    raise NotImplementedError("B3 render")


def render(lesson: Lesson) -> str:
    raise NotImplementedError("B3 render")
