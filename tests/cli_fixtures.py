"""Fixtures for the CLI tests, loaded as a pytest plugin by each test module."""

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_helpers import init_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized scars home with one registered repo, as SCARS_HOME, with TZ=UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    for name in ("SCARS_OFF", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    scars_home = tmp_path / "scars"
    monkeypatch.setenv("SCARS_HOME", str(scars_home))
    init_home(scars_home)
    return scars_home


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temp dir under /tmp: the secret scan's entropy rule flags the long random segment of macOS's
    default temp dir, and the miner would then store no working directory."""

    root = Path(tempfile.mkdtemp(prefix="scars-cli-", dir="/tmp")).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def user_home(tmp_path: Path) -> Path:
    path = tmp_path / "user"
    path.mkdir()
    return path
