"""A guard over the whole run: no test may create or change scars's files in the real home.

The real home comes from the password database, not `$HOME`, which the tests point at temp dirs. The
guard watches every file under `~/.scars` and the exact files scars or `claude plugin` write elsewhere.
It does not watch all of `~/.claude` or `~/.codex`: agent sessions running on the same machine change
files there all the time, and the guard would fail for reasons unrelated to the tests.
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


def real_home_state(
    trees: tuple[Path, ...] = WATCHED_TREES, files: tuple[Path, ...] = WATCHED_FILES
) -> dict[str, tuple[int, int] | None]:
    """Each watched path's (mtime in ns, size), or None when it does not exist."""
    state: dict[str, tuple[int, int] | None] = {}
    for path in (*files, *trees):
        try:
            st = path.stat()
        except OSError:
            state[str(path)] = None
            continue
        state[str(path)] = (st.st_mtime_ns, st.st_size)
    for tree in trees:
        if tree.is_dir():
            for path in tree.rglob("*"):
                try:
                    st = path.lstat()
                except OSError:
                    continue
                state[str(path)] = (st.st_mtime_ns, st.st_size)
    return state


def changed(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


@pytest.fixture(scope="session", autouse=True)
def real_home_untouched() -> Iterator[None]:
    before = real_home_state()
    yield
    assert changed(before, real_home_state()) == [], "the tests changed files in the real home"
