from cli_helpers import DAY, NOW, SIG, connect, human, make_ctx, run, seed
from scars import store, telemetry
from scars.model import EventType, HitOutcome

pytest_plugins = ("cli_fixtures",)


def mined(tool_use_id, command, status, sig=None, at=NOW - DAY):
    return store.MinedCall(
        tool_use_id, "claude", "s9", at, "Bash", "shell", command, (), status, sig, "/r", None
    )


def add_calls(home, calls):
    db = connect(home)
    store.commit_batch(db, offsets=(), seen=(), pending=(), calls=calls, prune_before=0.0)
    db.close()


def shown_hit(home, lesson_id, tool_use_id="tu-hook", at=NOW - 600):
    tele = telemetry.connect(str(home / "telemetry.db"))
    hit = telemetry.Hit(
        at, "claude", "2.1.299", None, "s9", tool_use_id, "tool_failed", "repair", lesson_id, 2,
        "provisional",
        f"error:repair:{SIG}", HitOutcome.SHOWN, 120, 12.5, None, None,
    )  # fmt: skip
    assert telemetry.write(tele, telemetry.Writes((hit,)))
    tele.close()


def test_ls_lists_active_lessons(home, user_home):
    a = seed(home)
    b = seed(
        home, avoid="npm tset", sig="npm ERR! Unknown command: tset", pre_trigger=None, provisional=False
    )
    human(home, a, EventType.PROMOTED)
    human(home, a, EventType.PINNED)
    lines = sorted(
        [
            f"{a:<11} promoted    note pin    1  repo app  pytest --frobnicate",
            f"{b:<11} candidate   note -      1  repo app  npm tset",
        ]
    )
    assert run(make_ctx(user_home), "ls") == (
        0,
        "ID          STATUS      LEVEL PIN SEEN  SCOPE  AVOID\n" + "\n".join(lines) + "\n",
        "",
    )


def test_ls_filters_by_status_repo_firing_and_stale(home, user_home):
    a = seed(home)
    b = seed(home, avoid="npm tset", sig="npm ERR! Unknown command: tset", at=NOW - 40 * DAY)
    human(home, b, EventType.RETIRED)
    shown_hit(home, a)
    assert run(make_ctx(user_home), "ls")[1].count("\n") == 2
    assert b in run(make_ctx(user_home), "ls", "--status", "retired")[1]
    assert run(make_ctx(user_home), "ls", "--status", "all")[1].count("\n") == 3
    assert run(make_ctx(user_home), "ls", "--repo", "other") == (0, "no lessons\n", "")
    assert a in run(make_ctx(user_home), "ls", "--firing")[1]
    assert run(make_ctx(user_home), "ls", "--stale") == (0, "no lessons\n", "")
    stale = run(make_ctx(user_home), "ls", "--stale", "--status", "all")[1]
    assert (a in stale, b in stale) == (False, True)


def test_why_shows_trigger_input_observations_and_events(home, user_home):
    lesson_id = seed(home)
    add_calls(home, [mined("tu-hook", "pytest --frobnicate tests/unit", "failed", SIG, at=NOW - 600)])
    shown_hit(home, lesson_id)
    header = f"{lesson_id}  provisional  level note  not pinned  repo app  trust own_code  seen in 1 session"
    expected = f"""{header}
  avoid:   pytest --frobnicate
  instead: run `pytest tests` instead
  reason:  {SIG}
triggers:
  command at gate: pytest ** --frobnicate
  error at repair: {SIG}
last fired: 2027-01-15 07:50 claude tool_failed in session s9
  matched trigger: error:repair:{SIG}
  matched input: pytest --frobnicate tests/unit
observations: 1
  2027-01-14 08:00 claude session s1
    failed: pytest --frobnicate tests
    error:  {SIG}
    passed: pytest tests
events:
  2027-01-14 08:00 proposed by miner (revision - -> 1)
  2027-01-14 08:00 provisionalized by miner (revision 1 -> 2)
"""
    assert run(make_ctx(user_home), "why", lesson_id) == (0, expected, "")
    assert run(make_ctx(user_home), "why", "last") == (0, expected, "")


def test_why_without_a_hit_or_mined_input(home, user_home):
    lesson_id = seed(home, pre_trigger=None)
    out = run(make_ctx(user_home), "why", lesson_id)[1]
    assert "last fired: never\n" in out
    shown_hit(home, lesson_id, tool_use_id="tu-unmined")
    assert (
        "  matched input: not recorded yet (scars mine reads it from the transcript)\n"
        in run(make_ctx(user_home), "why", lesson_id)[1]
    )


def test_why_last_with_no_hits_is_a_user_error(home, user_home):
    assert run(make_ctx(user_home), "why", "last") == (1, "", "scars why: no lesson has been shown yet\n")


def test_test_replays_the_pre_trigger_and_records_the_run(home, user_home):
    lesson_id = seed(home)
    add_calls(
        home,
        [
            mined("c1", "pytest --frobnicate a", "failed", SIG),
            mined("c2", "pytest --frobnicate b", "failed", SIG),
            mined("c3", "pytest --frobnicate c", "failed", SIG),
            mined("c4", "pytest --frobnicate d", "ok"),
            mined("c5", "pytest tests", "ok"),
            mined("c6", "pytest --frobnicate old", "failed", SIG, at=NOW - 31 * DAY),
        ],
    )
    assert run(make_ctx(user_home), "test", lesson_id) == (
        0,
        f"{lesson_id}: replayed 5 mined calls from the last 30 days\n"
        "  command at gate: pytest ** --frobnicate\n"
        "    matched_failing 3  matched_passing 1  matched 4\n"
        "    keep rule (at most 4 passing per failing): yes\n"
        "    block rule (at least 3 failing, under 1% passing): no\n",
        "",
    )
    row = (
        connect(home)
        .execute("select revision, matched_failing, matched_passing from precision_runs")
        .fetchall()
    )
    assert [tuple(r) for r in row] == [(2, 3, 1)]


def test_test_without_a_pre_trigger(home, user_home):
    lesson_id = seed(home, pre_trigger=None)
    assert run(make_ctx(user_home), "test", lesson_id) == (
        0,
        f"{lesson_id} has no command or path trigger with an error trigger; nothing to test\n",
        "",
    )


def test_commands_need_a_store(tmp_path, monkeypatch, user_home):
    monkeypatch.setenv("SCARS_HOME", str(tmp_path / "missing"))
    assert run(make_ctx(user_home), "ls") == (
        1,
        "",
        f"scars ls: no scars store at {tmp_path / 'missing'}; run scars init\n",
    )
