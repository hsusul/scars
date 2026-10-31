"""A guard over the whole run: no test may create or change scars's files in the real home.

The real home comes from the password database, not `$HOME`, which the tests point at temp dirs. The
guard watches every file under `~/.scars` and the exact files scars or `claude plugin` write elsewhere.
It does not watch all of `~/.claude` or `~/.codex`: agent sessions running on the same machine change
files there all the time, and the guard would fail for reasons unrelated to the tests. For the same
reason it skips the files a live scars install writes on every hook run (telemetry, SQLite's shared
memory and write-ahead files, logs), and compares directories by the names they hold, not their mtime.
"""

import os
import pwd
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
WATCHED_TREES = (REAL_HOME / ".scars", REAL_HOME / "Library" / "LaunchAgents")
WATCHED_FILES = (
    REAL_HOME / ".codex" / "hooks.json",
    REAL_HOME / ".claude" / "settings.json",
    REAL_HOME / ".claude" / "plugins" / "installed_plugins.json",
    REAL_HOME / ".claude" / "plugins" / "known_marketplaces.json",
)
LIVE_NAMES = frozenset(
    {"telemetry.db", "telemetry.db-wal", "telemetry.db-shm", "lessons.db-wal", "lessons.db-shm", "logs"}
)


def _live(path: Path, tree: Path) -> bool:
    return any(part in LIVE_NAMES for part in path.relative_to(tree).parts)


def _entry(path: Path, st: os.stat_result) -> object:
    if path.is_dir() and not path.is_symlink():
        return tuple(sorted(name for name in os.listdir(path) if name not in LIVE_NAMES))
    return (st.st_mtime_ns, st.st_size)


def real_home_state(
    trees: tuple[Path, ...] = WATCHED_TREES, files: tuple[Path, ...] = WATCHED_FILES
) -> dict[str, object]:
    """Each watched file's (mtime in ns, size) and each directory's entry names, or None when absent."""
    state: dict[str, object] = {}
    for path in (*files, *trees):
        try:
            st = path.stat()
        except OSError:
            state[str(path)] = None
            continue
        state[str(path)] = _entry(path, st)
    for tree in trees:
        if tree.is_dir():
            for path in tree.rglob("*"):
                if _live(path, tree):
                    continue
                try:
                    st = path.lstat()
                except OSError:
                    continue
                state[str(path)] = _entry(path, st)
    return state


def changed(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


@pytest.fixture(scope="session", autouse=True)
def real_home_untouched() -> Iterator[None]:
    before = real_home_state()
    yield
    assert changed(before, real_home_state()) == [], "the tests changed files in the real home"
