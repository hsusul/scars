import re

from cli_helpers import NOW, connect, first_telemetry, make_ctx, run, seed, telemetry_version
from scars import telemetry
from scars.commands import doctor, install
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
    from scars import paths

    install.shim_file(user_home, paths.home(), "/bin/scars-hook").write()
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
        f"ok    hook store: {home}\n"
        "ok    kill switch: off\n"
        f"ok    store location: {home}\n"
        "ok    event chain: hash chain and replay match the lessons\n"
        "ok    lessons render: every active lesson renders\n"
        "ok    canary: lesson returned in 7 ms\n",
        "",
    )


def test_doctor_fails_on_an_active_lesson_that_cannot_render(home, user_home, monkeypatch):
    stub(monkeypatch)
    seed(home)
    legacy = seed(
        home,
        avoid="npm tset",
        passing="npm test",
        sig="npm ERR! Unknown command: tset",
        reason="npm ERR! tset",
    )
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 1
    assert f"FAIL  lessons render: {legacy}: mined.reason_template in reason\n" in out


def test_doctor_fails_on_a_promoted_lesson_that_names_a_secret_argument(home, user_home, monkeypatch):
    from cli_helpers import human
    from scars.model import EventType

    stub(monkeypatch)
    lesson_id = seed(home, avoid="pytest --frobnicate --token hunter2")
    human(home, lesson_id, EventType.PROMOTED)
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 1
    assert f"FAIL  lessons render: {lesson_id}: secret.keyed_argument in avoid\n" in out
    assert "hunter2" not in out


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
        f"FAIL  lessons render: {lesson_id}: mined.program_mismatch in instead",
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


def test_ignored_events_do_not_count_toward_the_error_rate(home, user_home, monkeypatch):
    stub(monkeypatch)
    install_codex(user_home)
    seed(home)
    write_hits(
        home, [hit("codex", 5.0, HitOutcome.ERROR, "Boom")] + [hit("codex", 5.0, HitOutcome.IGNORED)] * 999
    )
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 1
    assert out.splitlines()[1:3] == [
        "codex: last hook 2027-01-15 07:59, 1000 runs, p50 5.0 ms, p99 5.0 ms, errors 1 (100.0%), "
        "error classes: Boom 1; hooks installed",
        "FAIL  codex hook errors: more than 1% of runs failed in the last 24 hours",
    ]


OLD_SCHEMA = (
    "ok    telemetry schema: version 1 is older than this scars (version 2); "
    "scars mine or scars init upgrades it"
)


def test_doctor_reports_an_older_telemetry_schema_without_writing_it(home, user_home, monkeypatch):
    stub(monkeypatch)
    install_codex(user_home)
    seed(home)
    (home / "telemetry.db").unlink()
    first_telemetry(home / "telemetry.db")
    before = (home / "telemetry.db").read_bytes()
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 0
    assert out.splitlines()[:3] == [
        OLD_SCHEMA,
        "claude: no hook runs in the last 24 hours; hooks not installed (run scars init)",
        "codex: last hook 2027-01-15 07:59, 1 runs, no latency, errors 0 (0.0%), error classes: none; "
        "hooks installed",
    ]
    assert (home / "telemetry.db").read_bytes() == before
    assert telemetry_version(home / "telemetry.db") == 1


def test_an_older_telemetry_schema_in_a_read_only_store_does_not_fail_doctor(home, user_home, monkeypatch):
    stub(monkeypatch)
    install_codex(user_home)
    seed(home)
    (home / "telemetry.db").unlink()
    first_telemetry(home / "telemetry.db")
    (home / "telemetry.db").chmod(0o400)
    home.chmod(0o500)
    try:
        code, out, _ = run(make_ctx(user_home), "doctor")
    finally:
        home.chmod(0o700)
        (home / "telemetry.db").chmod(0o600)
    assert code == 0
    assert out.splitlines()[0] == OLD_SCHEMA
    assert "FAIL  telemetry" not in out
    assert telemetry_version(home / "telemetry.db") == 1
