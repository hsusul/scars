import os
from pathlib import Path


def home() -> Path:
    """The scars state directory. `SCARS_HOME` overrides `~/.scars`, which tests rely on."""
    return Path(os.environ.get("SCARS_HOME") or Path.home() / ".scars")


def lessons_db() -> Path:
    return home() / "lessons.db"


def telemetry_db() -> Path:
    return home() / "telemetry.db"


def kill_switch() -> Path:
    return home() / "OFF"


def is_off() -> bool:
    return bool(os.environ.get("SCARS_OFF")) or kill_switch().exists()
