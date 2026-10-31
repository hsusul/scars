"""Where scars keeps its state.

The `*_path` functions return plain strings and use only `os.path`, so the hook can find its files without
importing `pathlib`, which costs it about 4 ms. The `Path` functions are for everything else.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def home_path() -> str:
    """The scars state directory. `SCARS_HOME` overrides `~/.scars`, which tests rely on."""
    return os.environ.get("SCARS_HOME") or os.path.join(os.path.expanduser("~"), ".scars")


class NotUtf8(ValueError):
    """An environment variable, argument or path whose bytes are not UTF-8 text."""


def text(value: str, name: str = "a path") -> str:
    """`value` as UTF-8 text, for printing it or writing it into a file.

    Python decodes environment variables, arguments and file names with the locale's encoding, so under
    an ASCII locale the UTF-8 bytes of `/Users/j\u00fcrgen` arrive as surrogate escapes, which no text
    encoder accepts. Those bytes are decoded again as UTF-8 here. File operations keep using the value
    Python gave, because under that locale only the escaped form encodes back to the same bytes.
    """
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        pass
    try:
        return value.encode("utf-8", "surrogateescape").decode("utf-8")
    except UnicodeError:
        raise NotUtf8(f"{name} is not valid UTF-8") from None


ENV_PATHS = ("HOME", "SCARS_HOME")


def check_env() -> None:
    """Raise `NotUtf8` naming the first of `ENV_PATHS` whose bytes are not UTF-8."""
    for name in ENV_PATHS:
        if value := os.environ.get(name):
            text(value, name)


def home() -> Path:
    from pathlib import Path

    return Path(home_path())


def lessons_db() -> Path:
    return home() / "lessons.db"


def telemetry_db() -> Path:
    return home() / "telemetry.db"


def kill_switch() -> Path:
    return home() / "OFF"


def is_off() -> bool:
    return bool(os.environ.get("SCARS_OFF")) or os.path.exists(os.path.join(home_path(), "OFF"))
