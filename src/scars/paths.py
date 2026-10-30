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
