import json
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
        f"unknown  codex sandbox: no {user_home}/.codex/config.toml\n"
        "ok    kill switch: off\n"
        "ok    delivery: lessons are shown\n"
        f"ok    store location: {home}\n"
        "ok    event chain: hash chain and replay match the lessons\n"
        "ok    lessons render: every active lesson renders\n"
        "ok    signature rules: every error trigger follows the current rules\n"
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


def test_a_promoted_lesson_from_older_signature_rules_is_kept_and_named_for_review(
    home, user_home, monkeypatch
):
    from cli_helpers import human
    from scars.model import EventType

    stub(monkeypatch)
    lesson_id = seed(home)
    human(home, lesson_id, EventType.PROMOTED)
    connect(home).execute("drop table if exists signature_rules")

    def lesson_rows():
        return [tuple(r) for r in connect(home).execute("select * from lessons")]

    before = lesson_rows()
    assert run(make_ctx(user_home), "mine")[1].splitlines()[2] == (
        "signature rules changed (version 1 to 4): 0 pending failures dropped, 0 failed calls unpaired, "
        "0 lessons retired, 1 to review (scars doctor names them)"
    )
    assert lesson_rows() == before
    code, out, _ = run(make_ctx(user_home), "doctor")
    assert code == 1
    assert f"FAIL  signature rules: {lesson_id}: error trigger made by older signature rules\n" in out
    assert run(make_ctx(user_home), "ls")[1].splitlines()[-1] == (
        f"needs review: lesson {lesson_id} has an error trigger made by older signature rules"
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
        "ok    delivery: lessons are shown",
        f"FAIL  store location: {home}: it is on an external disk",
        f"FAIL  event chain: {lesson_id}: avoid is tampered in lessons but pytest --frobnicate from events",
        f"FAIL  lessons render: {lesson_id}: mined.program_mismatch in instead",
        "ok    signature rules: every error trigger follows the current rules",
        "FAIL  canary: no lesson",
    ]


def test_doctor_names_the_log_only_baseline_and_the_arms(home, user_home, monkeypatch):
    stub(monkeypatch)
    seed(home)
    (home / "ARMS").touch()
    out = run(make_ctx(user_home), "doctor")[1]
    assert (
        "ok    delivery: arms (ARMS): each session is on or off by its id; off sessions record matches "
        "and show nothing\n" in out
    )
    (home / "BASELINE").touch()
    out = run(make_ctx(user_home), "doctor")[1]
    assert "ok    delivery: log-only baseline (BASELINE): hooks record matches and show nothing\n" in out


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


MANAGED = "system/Library/Application Support/ClaudeCode/managed-settings.json"


def sandbox(home, user_home, monkeypatch, *, claude=None, codex=None, managed=None):
    """Doctor's exit code and sandbox lines, with codex hooks installed so nothing else fails."""
    stub(monkeypatch)
    install_codex(user_home)
    seed(home)
    if claude is not None:
        (user_home / ".claude").mkdir()
        (user_home / ".claude" / "settings.json").write_text(claude)
    if codex is not None:
        (user_home / ".codex" / "config.toml").write_text(codex)
    if managed is not None:
        path = user_home.parent / MANAGED
        path.parent.mkdir(parents=True)
        path.write_text(managed)
    code, out, _ = run(make_ctx(user_home), "doctor")
    return code, [line for line in out.splitlines() if " sandbox: " in line]


CODEX_UNKNOWN = "unknown  codex sandbox: no {}/.codex/config.toml"


def test_claude_sandbox_that_keeps_writes_away_from_the_store_is_ok(home, user_home, monkeypatch):
    claude = '{"sandbox": {"enabled": true, "filesystem": {"allowWrite": ["~/.kube", "/tmp/build"]}}}'
    assert sandbox(home, user_home, monkeypatch, claude=claude) == (
        0,
        [f"ok    claude sandbox: on, and no write rule reaches {home}", CODEX_UNKNOWN.format(user_home)],
    )


def test_claude_sandbox_turned_off_fails(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, claude='{"sandbox": {"enabled": false}}') == (
        1,
        [
            f"FAIL  claude sandbox: sandbox.enabled is false in {user_home}/.claude/settings.json, "
            f"so Bash commands can write {home}",
            CODEX_UNKNOWN.format(user_home),
        ],
    )


def test_claude_sandbox_with_filesystem_isolation_off_fails(home, user_home, monkeypatch):
    claude = '{"sandbox": {"enabled": true, "filesystem": {"disabled": true}}}'
    assert sandbox(home, user_home, monkeypatch, claude=claude)[1][0] == (
        f"FAIL  claude sandbox: sandbox.filesystem.disabled is true in {user_home}/.claude/settings.json, "
        f"so Bash commands can write {home}"
    )


def test_claude_sandbox_that_allows_writing_the_store_fails(home, user_home, monkeypatch):
    claude = json.dumps({"sandbox": {"enabled": True, "filesystem": {"allowWrite": [f"{home.parent}/**"]}}})
    assert sandbox(home, user_home, monkeypatch, claude=claude) == (
        1,
        [
            f"FAIL  claude sandbox: sandbox.filesystem.allowWrite {home.parent}/** in "
            f"{user_home}/.claude/settings.json lets Bash commands write {home}",
            CODEX_UNKNOWN.format(user_home),
        ],
    )


def test_claude_edit_allow_rule_over_the_store_fails(home, user_home, monkeypatch):
    claude = json.dumps(
        {"sandbox": {"enabled": True}, "permissions": {"allow": ["Bash(ls)", f"Edit(/{home})"]}}
    )
    assert sandbox(home, user_home, monkeypatch, claude=claude)[1][0] == (
        f"FAIL  claude sandbox: permissions.allow Edit(/{home}) in {user_home}/.claude/settings.json "
        f"lets Bash commands write {home}"
    )


def test_claude_deny_write_on_the_store_wins_over_an_allow(home, user_home, monkeypatch):
    claude = json.dumps(
        {"sandbox": {"enabled": True, "filesystem": {"allowWrite": ["/"], "denyWrite": [str(home)]}}}
    )
    assert sandbox(home, user_home, monkeypatch, claude=claude)[1][0] == (
        f"ok    claude sandbox: on, and sandbox.filesystem.denyWrite {home} in "
        f"{user_home}/.claude/settings.json denies writes to {home}"
    )


def test_claude_settings_without_the_sandbox_key_are_unknown_and_do_not_fail(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, claude='{"theme": "dark"}') == (
        0,
        [
            f"unknown  claude sandbox: no sandbox.enabled in {user_home}/.claude/settings.json",
            CODEX_UNKNOWN.format(user_home),
        ],
    )


def test_claude_settings_that_do_not_parse_are_unknown(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, claude='{"sandbox": ')[1][0] == (
        f"unknown  claude sandbox: cannot parse {user_home}/.claude/settings.json: "
        "Expecting value: line 1 column 13 (char 12)"
    )


def test_claude_managed_settings_decide_whether_the_sandbox_is_on(home, user_home, monkeypatch):
    managed = '{"sandbox": {"enabled": true}}'
    claude = '{"sandbox": {"enabled": false}}'
    assert sandbox(home, user_home, monkeypatch, claude=claude, managed=managed)[1][0] == (
        f"ok    claude sandbox: on, and no write rule reaches {home}"
    )


def test_codex_read_only_sandbox_is_ok(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, codex='sandbox_mode = "read-only"\n') == (
        0,
        [f"ok    codex sandbox: sandbox_mode is read-only in {user_home}/.codex/config.toml"],
    )


def test_codex_workspace_write_away_from_the_store_is_ok(home, user_home, monkeypatch):
    codex = 'sandbox_mode = "workspace-write"\n\n[sandbox_workspace_write]\nwritable_roots = ["/tmp/build"]\n'
    assert sandbox(home, user_home, monkeypatch, codex=codex) == (
        0,
        [
            f"ok    codex sandbox: sandbox_mode is workspace-write in {user_home}/.codex/config.toml, "
            f"and no writable root contains {home}"
        ],
    )


def test_codex_full_access_fails(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, codex='sandbox_mode = "danger-full-access"\n') == (
        1,
        [
            f"FAIL  codex sandbox: sandbox_mode is danger-full-access in {user_home}/.codex/config.toml, "
            f"so agents can write {home}"
        ],
    )


def test_codex_writable_root_that_holds_the_store_fails(home, user_home, monkeypatch):
    codex = (
        f"sandbox_mode = 'workspace-write'\n[sandbox_workspace_write]\nwritable_roots = ['{home.parent}']\n"
    )
    assert sandbox(home, user_home, monkeypatch, codex=codex) == (
        1,
        [
            f"FAIL  codex sandbox: writable root {home.parent} in {user_home}/.codex/config.toml "
            f"contains {home}"
        ],
    )


def test_codex_selected_profile_sets_the_sandbox_mode(home, user_home, monkeypatch):
    codex = (
        'profile = "yolo"\nsandbox_mode = "read-only"\n\n'
        '[profiles.yolo]\nsandbox_mode = "danger-full-access"\n'
    )
    assert sandbox(home, user_home, monkeypatch, codex=codex)[1] == [
        f"FAIL  codex sandbox: profiles.yolo.sandbox_mode is danger-full-access in "
        f"{user_home}/.codex/config.toml, so agents can write {home}"
    ]


def test_codex_config_without_a_sandbox_mode_is_unknown_and_does_not_fail(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, codex='model = "gpt-5"\n') == (
        0,
        [f"unknown  codex sandbox: no sandbox_mode in {user_home}/.codex/config.toml"],
    )


def test_codex_config_that_does_not_parse_is_unknown(home, user_home, monkeypatch):
    assert sandbox(home, user_home, monkeypatch, codex="sandbox_mode = \n") == (
        0,
        [
            f"unknown  codex sandbox: cannot parse {user_home}/.codex/config.toml: "
            "Invalid value (at line 1, column 16)"
        ],
    )
