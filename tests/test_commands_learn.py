import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from cli_helpers import NOW, SIG, connect, make_ctx, run, seed
from scars import learn, store
from scars.learn import Keep, Record, Reject, Unavailable, Verdict
from scars.model import EventType, LessonId, LessonText

pytest_plugins = ("cli_fixtures",)

STUB_BIN = Path(__file__).parent / "fixtures" / "bin"
KEPT = LessonText(
    "Passing a simulator device id copied from an earlier session",
    "Omit the device argument so the tool picks the booted simulator",
    "Simulator ids change between sessions, so an old one no longer exists",
)


def seen_in(home: Path, lesson_id: LessonId, sessions: int, failed: str, passed: str) -> None:
    """Give a seeded lesson observations from `sessions` sessions in all, the way the miner does."""
    db = connect(home)
    for n in range(2, sessions + 1):
        at = NOW - 3600 * (10 - n)
        lesson = store.get_lesson(db, lesson_id)
        assert lesson is not None
        store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.OBSERVED,
            expected_revision=lesson.revision,
            actor="miner",
            payload={"fix_program": "pytest"},
            at=at,
        )
        obs = store.Observation(lesson_id, "claude", f"s{n}", f"tu-{lesson_id}-{n}", failed, SIG, passed, at)
        store.add_observation(db, obs)
    db.close()


@pytest.fixture
def two(home: Path) -> tuple[LessonId, LessonId]:
    """Two candidates seen in 3 and 2 sessions, plus one seen once and one provisional, which it skips."""
    best = seed(home, provisional=False)
    seen_in(home, best, 3, "pytest --frobnicate tests/unit", "pytest tests/unit")
    second = seed(home, avoid="pytest --frob", passing="pytest -q", provisional=False)
    seen_in(home, second, 2, "pytest --frob tests", "pytest -q")
    seed(home, avoid="pytest --once", passing="pytest -x", provisional=False)
    provisional = seed(home, avoid="pytest --later", passing="pytest -k later")
    seen_in(home, provisional, 2, "pytest --later tests", "pytest -k later")
    return best, second


def store_state(home: Path) -> tuple[str, int]:
    """The checksum of lessons.db and the bytes in its write-ahead log (a read-only open may leave an
    empty one behind; any written page would land there first)."""
    wal = home / "lessons.db-wal"
    return hashlib.sha256(
        (home / "lessons.db").read_bytes()
    ).hexdigest(), wal.stat().st_size if wal.exists() else 0


class Stub:
    """An `ask_model` that answers from a table, or with `default` for a lesson not in it."""

    def __init__(self, verdicts: dict[LessonId, Verdict], default: Verdict | None = None) -> None:
        self.verdicts = verdicts
        self.default = default
        self.asked: list[Record] = []

    def __call__(self, record: Record) -> Verdict:
        self.asked.append(record)
        found = self.verdicts.get(record.lesson_id, self.default)
        assert found is not None, record.lesson_id
        return found


def test_dry_run_prints_each_verdict_best_first_and_writes_nothing(home, user_home, two):
    best, second = two
    stub = Stub({best: Keep(KEPT), second: Reject("The fix only read a different file")})
    ctx = make_ctx(user_home)
    ctx.ask_model = stub
    before = store_state(home)

    assert run(ctx, "learn", "--dry-run") == (
        0,
        f"{best}  keep  avoid: Passing a simulator device id copied from an earlier session\n"
        "  instead: Omit the device argument so the tool picks the booted simulator\n"
        "  reason: Simulator ids change between sessions, so an old one no longer exists\n"
        f"{second}  reject  The fix only read a different file\n",
        "",
    )
    assert store_state(home) == before
    assert [(r.lesson_id, r.failed, r.passed, r.sessions_seen, r.tool) for r in stub.asked] == [
        (best, "pytest --frobnicate tests/unit", "pytest tests/unit", 3, "Bash"),
        (second, "pytest --frob tests", "pytest -q", 2, "Bash"),
    ]


def test_dry_run_prints_unavailable_per_candidate_and_hides_unsafe_reject_text(home, user_home, two):
    best, second = two
    ctx = make_ctx(user_home)
    ctx.ask_model = Stub(
        {best: Unavailable("model text refused: sanitizer.url"), second: Reject("bad \x1b[31m")}
    )

    assert run(ctx, "learn", "--dry-run") == (
        0,
        f"{best}  unavailable  model text refused: sanitizer.url\n"
        f"{second}  reject  [hidden: sanitizer.ansi_escape]\n",
        "",
    )


def test_when_claude_cannot_answer_at_all_it_says_so_once_and_stops(home, user_home, two):
    best, second = two
    stub = Stub({best: Unavailable("not logged in; run claude auth login"), second: Keep(KEPT)})
    ctx = make_ctx(user_home)
    ctx.ask_model = stub

    assert run(ctx, "learn", "--dry-run") == (
        0,
        "claude unavailable: not logged in; run claude auth login\n",
        "",
    )
    assert [r.lesson_id for r in stub.asked] == [best]


def test_no_qualifying_candidates(home, user_home):
    seed(home, provisional=False)
    ctx = make_ctx(user_home)
    ctx.ask_model = Stub({})
    assert run(ctx, "learn", "--dry-run") == (0, "no candidates seen in 2 or more sessions\n", "")


def test_at_most_five_candidates_are_asked(home, user_home):
    for n in range(7):
        lesson_id = seed(home, avoid=f"pytest --frob{n}", passing=f"pytest -k t{n}", provisional=False)
        seen_in(home, lesson_id, 2, f"pytest --frob{n} tests", f"pytest -k t{n}")
    stub = Stub({}, default=Reject("noise"))
    ctx = make_ctx(user_home)
    ctx.ask_model = stub
    code, out, _ = run(ctx, "learn", "--dry-run")
    assert (code, len(stub.asked), out.count("  reject  noise\n")) == (0, 5, 5)


def test_learn_without_dry_run_is_not_available_yet(home, user_home, two):
    ctx = make_ctx(user_home)
    ctx.ask_model = Stub({})
    assert run(ctx, "learn") == (2, "", "scars learn: only --dry-run is available\n")


@pytest.fixture
def stub_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{STUB_BIN}{os.pathsep}/usr/bin{os.pathsep}/bin")
    args = tmp_path / "args"
    monkeypatch.setenv("SCARS_STUB_ARGS", str(args))
    return args


def test_dry_run_through_the_claude_binary_on_path(home, user_home, two, stub_cli, monkeypatch):
    best, second = two
    monkeypatch.setenv("SCARS_STUB_VERDICT", "keep")
    before = store_state(home)

    assert run(make_ctx(user_home), "learn", "--dry-run") == (
        0,
        "".join(
            f"{lesson_id}  keep  avoid: Passing a simulator device id copied from an earlier session\n"
            "  instead: Omit the device argument so the tool picks the booted simulator\n"
            "  reason: Simulator ids change between sessions, so an old one no longer exists\n"
            for lesson_id in (best, second)
        ),
        "",
    )
    assert store_state(home) == before
    argv = [a.decode() for a in stub_cli.read_bytes().split(b"\0")[:-1]]
    assert argv[0] == "-p" and argv[1].startswith("<record>\n") and argv[1].endswith("\n</record>")
    assert argv[2:] == [
        "--model",
        "haiku",
        "--system-prompt",
        learn.SYSTEM,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(learn.SCHEMA),
    ]


@pytest.mark.parametrize(
    ("verdict", "out"),
    [
        ("error", "claude unavailable: not logged in; run claude auth login\n"),
        (
            "malformed",
            "{best}  unavailable  malformed model output\n{second}  unavailable  malformed model output\n",
        ),
        (
            "reject",
            "{best}  reject  The fix only read a different file\n"
            "{second}  reject  The fix only read a different file\n",
        ),
    ],
)
def test_the_claude_binary_on_path_maps_each_envelope(
    home, user_home, two, stub_cli, monkeypatch, verdict, out
):
    best, second = two
    monkeypatch.setenv("SCARS_STUB_VERDICT", verdict)
    assert run(make_ctx(user_home), "learn", "--dry-run") == (0, out.format(best=best, second=second), "")


def test_a_claude_that_never_answers_is_stopped_at_the_timeout(home, user_home, two, stub_cli, monkeypatch):
    monkeypatch.setenv("SCARS_STUB_VERDICT", "sleep")
    monkeypatch.setattr(learn, "TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    assert run(make_ctx(user_home), "learn", "--dry-run") == (
        0,
        "claude unavailable: no answer within 60 s\n",
        "",
    )
    assert time.monotonic() - started < 10


def test_no_claude_on_path(home, user_home, two, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert run(make_ctx(user_home), "learn", "--dry-run") == (
        0,
        "claude unavailable: claude not found on PATH\n",
        "",
    )
