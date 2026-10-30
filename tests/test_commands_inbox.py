import pytest

from cli_helpers import DAY, NOW, SIG, connect, make_ctx, run, seed
from scars import store
from scars.commands import UserError
from scars.commands.inbox import consistency, parse_edit, rank
from scars.model import Status, TrustClass

pytest_plugins = ("cli_fixtures",)


def card(lesson_id, position, total, score, avoid="pytest --frobnicate", passing="pytest tests", sig=SIG):
    return (
        f"\n[{position}/{total}] score {score}\n"
        f"{lesson_id}  provisional  level note  not pinned  repo app  trust own_code  seen in 1 session\n"
        f"  avoid:   {avoid}\n"
        f"  instead: run `{passing}` instead\n"
        f"  reason:  {sig}\n"
        f"  source:  `{avoid} tests` failed, then `{passing}` passed\n"
        "a approve  e edit  m mute  s skip  q quit\n"
    )


def test_empty_inbox(home, user_home):
    assert run(make_ctx(user_home), "inbox") == (0, "inbox is empty\n", "")


def test_approve_promotes_and_skip_moves_on(home, user_home):
    a = seed(home, at=NOW)
    b = seed(
        home, avoid="npm tset", passing="npm test", sig="npm ERR! Unknown command: tset", at=NOW - 14 * DAY
    )
    assert run(make_ctx(user_home, keys="as"), "inbox") == (
        0,
        card(a, 1, 2, "1.00")
        + f"promoted {a} (repo app, level note, not pinned)\n"
        + card(b, 2, 2, "0.50", avoid="npm tset", passing="npm test", sig="npm ERR! Unknown command: tset")
        + "inbox: 1 approved, 0 edited, 0 muted, 1 skipped; 1 waiting\n",
        "",
    )
    db = connect(home)
    assert (store.get_lesson(db, a).status, store.get_lesson(db, b).status) == (
        Status.PROMOTED,
        Status.PROVISIONAL,
    )


def test_mute_quit_and_unknown_keys(home, user_home):
    a = seed(home)
    out = run(make_ctx(user_home, keys="xmq"), "inbox")[1]
    assert out == card(a, 1, 1, "0.95") + "unknown key 'x'\n" + card(a, 1, 1, "0.95") + f"muted {a}\n" + (
        "inbox: 0 approved, 0 edited, 1 muted, 0 skipped; 0 waiting\n"
    )
    assert store.get_lesson(connect(home), a).status is Status.RETIRED


def test_inbox_shows_at_most_five(home, user_home):
    for i in range(7):
        seed(home, avoid=f"tool{i} --bad", passing=f"tool{i} --good", sig=f"tool{i}: error: bad flag")
    out = run(make_ctx(user_home, keys="q"), "inbox")[1]
    assert "[1/5]" in out
    assert out.endswith("inbox: 0 approved, 0 edited, 0 muted, 0 skipped; 7 waiting\n")


def test_edit_rewrites_the_text_as_human_and_shows_it_again(home, user_home):
    a = seed(home)
    seen = []

    def editor(text):
        seen.append(text)
        return text.replace("instead: run `pytest tests` instead", "instead: drop the flag; pytest has none")

    out = run(make_ctx(user_home, keys="ea", edit=editor), "inbox")[1]
    assert seen == [
        "# Rewrite the lesson. Lines starting with # are ignored. Keep each field on one line.\n"
        "avoid: pytest --frobnicate\n"
        "instead: run `pytest tests` instead\n"
        f"reason: {SIG}\n"
    ]
    assert f"edited {a}; it is a candidate again\n" in out
    assert f"{a}  candidate  level note  not pinned  repo app  trust human  seen in 1 session\n" in out
    assert out.endswith(f"promoted {a} (repo app, level note, not pinned)\n"
                        "inbox: 1 approved, 1 edited, 0 muted, 0 skipped; 0 waiting\n")  # fmt: skip
    db = connect(home)
    lesson = store.get_lesson(db, a)
    assert (lesson.status, lesson.trust_class, lesson.text.instead) == (
        Status.PROMOTED,
        TrustClass.HUMAN,
        "drop the flag; pytest has none",
    )
    assert [e.type for e in store.lesson_events(db, a)] == [
        "proposed",
        "provisionalized",
        "edited",
        "promoted",
    ]
    assert store.verify(db) == []


def test_an_edit_that_breaks_a_text_rule_changes_nothing(home, user_home):
    a = seed(home)
    out = run(
        make_ctx(
            user_home, keys="eq", edit=lambda t: t.replace("avoid: pytest --frobnicate", "avoid: x\x07y")
        ),
        "inbox",
    )[1]
    assert "not applied: avoid breaks sanitizer.control_char\n" in out
    assert store.get_lesson(connect(home), a).revision == 2


def test_inbox_refuses_to_apply_a_decision_without_a_terminal(home, user_home):
    a = seed(home)
    assert run(make_ctx(user_home, keys="a", tty=False), "inbox")[2] == (
        "scars inbox: refused: stdin and stdout must be a terminal; run it in your own terminal\n"
    )
    assert store.get_lesson(connect(home), a).status is Status.PROVISIONAL


def test_rank_scores_by_sessions_consistency_and_age(home):
    a = seed(home, at=NOW - 28 * DAY)
    db = connect(home)
    store.add_observation(
        db, store.Observation(a, "claude", "s2", "tu-2", "x", SIG, "make test", NOW - 28 * DAY)
    )
    [ranked] = rank(db, NOW)
    assert (ranked.lesson.id, ranked.score) == (a, 1 * 0.5 * 0.25)
    assert consistency(["pytest a", "pytest b", "make x", "'pytest' c"]) == 0.75


def test_parse_edit_rejects_missing_and_unknown_fields():
    assert parse_edit("# c\navoid: a\ninstead: b: c\nreason: r\n") == {
        "avoid": "a",
        "instead": "b: c",
        "reason": "r",
    }
    with pytest.raises(UserError, match="missing reason"):
        parse_edit("avoid: a\ninstead: b\n")
    with pytest.raises(UserError, match="unexpected line 'level: warn'"):
        parse_edit("level: warn\n")
