import re

from cli_helpers import NOW, connect, make_ctx, run, seed
from scars import telemetry
from scars.commands import doctor
from scars.model import HitOutcome

pytest_plugins = ("cli_fixtures",)


def hit(harness, latency, outcome=HitOutcome.NO_MATCH, error_class=None, at=NOW - 60):
    return telemetry.Hit(at, harness, None, None, "s", None, "tool_failed", "repair", None, None, None, None,
                         outcome, 0, latency, error_class, None)  # fmt: skip


def write_hits(home, hits):
    tele = telemetry.connect(str(home / "telemetry.db"))
    assert telemetry.write(tele, telemetry.Writes(tuple(hits)))
    tele.close()


HEALTHY_CANARY = doctor.Check("canary", True, "lesson returned in 7 ms")


def stub(monkeypatch, location=None, canary=HEALTHY_CANARY):
    monkeypatch.setattr(doctor, "store_location_problem", lambda _: location)
    monkeypatch.setattr(doctor, "run_canary", lambda: canary)


def install_codex(user_home):
    (user_home / ".codex").mkdir()
    (user_home / ".codex" / "hooks.json").write_text('{"hooks": {"x": "/bin/scars-hook codex x"}}')


def test_healthy_doctor(home, user_home, monkeypatch):
    stub(monkeypatch)
    install_codex(user_home)
    seed(home)
    write_hits(
        home, [hit("codex", float(ms)) for ms in range(1, 101)] + [hit("codex", 9.0, at=NOW - 2 * 86400)]
    )
    assert run(make_ctx(user_home), "doctor") == (
        0,
        "claude: no hook runs in the last 24 hours; hooks not installed (run scars init)\n"
        "codex: last hook 2027-01-15 07:59, 100 runs, p50 50.5 ms, p99 100.0 ms, errors 0 (0.0%), "
        "error classes: none; hooks installed\n"
        "ok    kill switch: off\n"
        f"ok    store location: {home}\n"
        "ok    event chain: hash chain and replay match the lessons\n"
        "ok    canary: lesson returned in 7 ms\n",
        "",
    )


def test_each_failing_check_makes_doctor_exit_1(home, user_home, monkeypatch):
    stub(monkeypatch, location="it is on an external disk", canary=doctor.Check("canary", False, "no lesson"))
    lesson_id = seed(home)
    connect(home).execute("update lessons set avoid = 'tampered' where id = ?", (lesson_id,))
    write_hits(
        home, [hit("claude", 5.0)] * 98 + [hit("claude", 900.0, HitOutcome.ERROR, "lessons_unavailable")] * 2
    )
    (home / "OFF").touch()
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 1
    assert out.splitlines() == [
        "claude: last hook 2027-01-15 07:59, 100 runs, p50 5.0 ms, p99 900.0 ms, errors 2 (2.0%), "
        "error classes: lessons_unavailable 2; hooks not installed (run scars init)",
        "FAIL  claude hook errors: more than 1% of runs failed in the last 24 hours",
        "codex: no hook runs in the last 24 hours; hooks not installed (run scars init)",
        "FAIL  hooks: no harness has scars hooks installed",
        "FAIL  kill switch: on: hooks do nothing (scars on)",
        f"FAIL  store location: {home}: it is on an external disk",
        f"FAIL  event chain: {lesson_id}: avoid is tampered in lessons but pytest --frobnicate from events",
        "FAIL  canary: no lesson",
    ]


def test_missing_store(tmp_path, monkeypatch, user_home):
    stub(monkeypatch)
    monkeypatch.setenv("SCARS_HOME", str(tmp_path / "none"))
    out = run(make_ctx(user_home), "doctor")[1]
    assert f"FAIL  store: no store at {tmp_path / 'none'}; run scars init\n" in out


def test_real_canary_returns_its_lesson_in_budget(monkeypatch):
    monkeypatch.setenv("SCARS_HOME", "/nonexistent-home")
    # The 100 ms budget is doctor's; a loaded CI runner gets more so the test checks delivery, not speed.
    check = doctor.run_canary(budget_ms=2000.0)
    assert check.ok, check.detail
    assert re.fullmatch(r"lesson returned in \d+ ms", check.detail)
    assert doctor.run_canary(budget_ms=0.0).detail.startswith("the canary lesson took ")
    import os

    assert os.environ["SCARS_HOME"] == "/nonexistent-home"


def test_synced_folders_are_flagged(tmp_path):
    synced = tmp_path / "Library" / "Mobile Documents" / "scars"
    assert doctor.store_location_problem(synced) == "it is inside a synced folder (Library/Mobile Documents)"
