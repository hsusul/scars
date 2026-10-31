"""`scars doctor` reports a broken store or telemetry file as a failed check and never crashes."""

import os
import subprocess

import pytest

from cli_helpers import make_ctx, run
from scars.commands import doctor

pytest_plugins = ("cli_fixtures",)


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(doctor, "store_location_problem", lambda _: None)
    monkeypatch.setattr(doctor, "run_canary", lambda: doctor.Check("canary", True, "lesson returned in 7 ms"))


def fail_lines(out):
    return [line for line in out.splitlines() if line.startswith("FAIL")]


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"", "cannot read the lessons store: no such table: lessons"),
        (
            b"this is not a database at all, just text" * 200,
            "cannot read the lessons store: file is not a database",
        ),
    ],
    ids=["empty", "corrupt"],
)
def test_doctor_fails_on_an_empty_or_corrupt_lessons_store(home, user_home, stubbed, content, detail):
    (home / "lessons.db").write_bytes(content)
    code, out, err = run(make_ctx(user_home), "doctor")
    assert (code, err) == (1, "")
    assert f"FAIL  event chain: {detail}" in fail_lines(out)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_doctor_fails_on_an_unreadable_lessons_store(home, user_home, stubbed):
    (home / "lessons.db").chmod(0)
    try:
        code, out, err = run(make_ctx(user_home), "doctor")
    finally:
        (home / "lessons.db").chmod(0o600)
    assert (code, err) == (1, "")
    assert "FAIL  event chain: cannot read the lessons store: unable to open database file" in fail_lines(out)


def test_doctor_fails_on_a_corrupt_telemetry_store(home, user_home, stubbed):
    (home / "telemetry.db").write_bytes(b"garbage" * 1000)
    code, out, err = run(make_ctx(user_home), "doctor")
    assert (code, err) == (1, "")
    assert "FAIL  telemetry: cannot read telemetry.db: file is not a database" in fail_lines(out)
    assert "ok    event chain: hash chain and replay match the lessons" in out


DF = (
    "Filesystem                   1024-blocks Used Available Capacity Mounted on\n"
    "//me@nas/Shared Docs            1000000 10 999990     1%    /Volumes/Shared Docs\n"
)
MOUNT = (
    "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
    "//me@nas/Shared Docs on /Volumes/Shared Docs (smbfs, nodev, nosuid, mounted by me)\n"
)


def test_a_file_system_name_with_spaces_cannot_hide_a_network_mount(monkeypatch, tmp_path):
    def fake_run(argv, **_):
        out = {"df": DF, "mount": MOUNT}.get(argv[0], "")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor.store_location_problem(tmp_path) == "it is on a network file system"
