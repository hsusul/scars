"""Bounded shell parser for command triggers. See ARCHITECTURE "Matching", "Shell parsing"."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SimpleCommand:
    program: str
    args: tuple[str, ...]
    cwd: str | None


class Unsupported(Exception):
    """The command uses syntax the parser does not handle, so command triggers are skipped."""


def parse(command: str) -> tuple[SimpleCommand, ...]:
    raise NotImplementedError("B2 shell")
