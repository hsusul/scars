"""Fixtures for the CLI tests, loaded as a pytest plugin by each test module.

Every test that loads them runs with `HOME` pointing at a temp dir, so nothing can reach the real home.
Temp dirs live under /tmp: the secret scan's entropy rule flags the long random segment of macOS's
default temp dir, so a path there would be hidden in read command output and stored as no working
directory by the miner.
"""

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_helpers import init_home


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="scars-cli-", dir="/tmp")).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`HOME` set to a temp dir for every test, before any other fixture runs."""
    path = tmp_path / "user"
    path.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def home(tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized scars home with one registered repo, as SCARS_HOME, with TZ=UTC."""
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    for name in ("SCARS_OFF", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    scars_home = tmp_path / "scars"
    monkeypatch.setenv("SCARS_HOME", str(scars_home))
    init_home(scars_home)
    return scars_home


@pytest.fixture
def work(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A second temp dir under /tmp, with `HOME` at its `user` dir (created by the tests that use it)."""
    root = Path(tempfile.mkdtemp(prefix="scars-cli-", dir="/tmp")).resolve()
    monkeypatch.setenv("HOME", str(root / "user"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def user_home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(isolated_home))
    return isolated_home
