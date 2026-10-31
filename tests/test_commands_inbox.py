import pytest

from cli_helpers import DAY, NOW, SIG, connect, make_ctx, run, seed
from scars import render, store
from scars.commands import UserError
from scars.commands.inbox import consistency, parse_edit, rank
from scars.model import (
    TEXT_CAP,
    LessonText,
    SessionId,
    SessionScope,
    Stage,
    Status,
    Trigger,
    TriggerKind,
    TrustClass,
)

pytest_plugins = ("cli_fixtures",)


def card(lesson_id, position, total, score, avoid="pytest --frobnicate", passing="pytest tests", sig=SIG):
    return (
        f"\n[{position}/{total}] score {score}\n"
        f"{lesson_id}  provisional  level note  not pinned  repo app  trust own_code  seen in 1 session\n"
        f"  avoid:   {avoid}\n"
        f"  instead: run `{passing}` instead\n"
        f"  reason:  {render.MINED_REASON}\n"
        "  source evidence, as recorded (agents never see it):\n"
        f"    failed: {avoid} tests\n"
        f"    error:  {sig}\n"
        f"    passed: {passing}\n"
        "a approve  e edit  m mute  s skip  q quit\n"
    )


HIDDEN = (
    "hidden: 1 that cannot render (scars doctor names the rule); 1 from a directory that is not a "
    "registered repo (scars init offers to register the repos your sessions used)\n"
)


def session_lesson(home):
    db = connect(home)
    lesson_id = store.create_lesson(
        db,
        text=LessonText("make tset", "run `make test` instead", render.MINED_REASON),
        scope=SessionScope(SessionId("s9")),
        trust_class=TrustClass.OWN_CODE,
        triggers=(Trigger(TriggerKind.ERROR, "make: No rule to make target tset", Stage.REPAIR),),
        fingerprint="fp-session",
        actor="miner",
        at=NOW - DAY,
    )
    store.add_observation(
        db,
        store.Observation(
            lesson_id,
            "claude",
            "s9",
            "tu-s9",
            "make tset",
            "make: No rule to make target tset",
            "make test",
            NOW,
        ),
    )
    db.commit()
    db.close()
    return lesson_id


def test_inbox_hides_what_it_cannot_render_or_approve_and_counts_only_the_rest(home, user_home):
    from scars.commands.mining import waiting_line

    a = seed(home)
    seed(home, avoid="npm tset", passing="npm test", sig="npm ERR! tset", reason="npm ERR! Unknown command")
    session_lesson(home)
    assert run(make_ctx(user_home, keys="q"), "inbox") == (
        0,
        HIDDEN + card(a, 1, 1, "0.95") + "inbox: 0 approved, 0 edited, 0 muted, 0 skipped; 1 waiting\n",
        "",
    )
    assert waiting_line(home) == "1 lesson waiting: run scars inbox"


def test_inbox_shows_source_evidence_with_secret_values_masked(home, user_home):
    a = seed(home)
    db = connect(home)
    db.execute("update observations set failed_input = 'pytest --frobnicate --token abc123 tests'")
    db.commit()
    out = run(make_ctx(user_home, keys="q"), "inbox")[1]
    assert "    failed: pytest --frobnicate --token *** tests\n" in out
    assert "abc123" not in out
    assert a in out


def test_inbox_hides_a_lesson_whose_text_names_a_secret_argument(home, user_home):
    a = seed(home, avoid="pytest --frobnicate --token hunter2")
    out = run(make_ctx(user_home, keys="a"), "inbox")[1]
    assert out == "hidden: 1 that cannot render (scars doctor names the rule)\ninbox is empty\n"
    assert store.get_lesson(connect(home), a).status is Status.PROVISIONAL


def test_an_edit_that_names_a_secret_argument_is_refused(home, user_home):
    a = seed(home)

    def editor(text):
        return text.replace("avoid: pytest --frobnicate", "avoid: pytest --frobnicate --token hunter2")

    out = run(make_ctx(user_home, keys="eyq", edit=editor), "inbox")[1]
    assert "not applied: avoid breaks secret.keyed_argument\n" in out
    assert "hunter2" not in out
    assert store.get_lesson(connect(home), a).text.avoid == "pytest --frobnicate"


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

    out = run(make_ctx(user_home, keys="eya", edit=editor), "inbox")[1]
    assert seen == [
        "# Rewrite the lesson. Lines starting with # are ignored. Keep each field on one line.\n"
        "avoid: pytest --frobnicate\n"
        "instead: run `pytest tests` instead\n"
        f"reason: {render.MINED_REASON}\n"
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


def preview(lesson_id, instead):
    return (
        "this is how agents will see the edited lesson:\n"
        f"[scars {lesson_id} note | repo app | seen 1x | unreviewed]\n"
        "AVOID: pytest --frobnicate\n"
        f"INSTEAD: {instead}\n"
        f"WHY: {render.MINED_REASON}\n"
        f"(advisory; scars why {lesson_id})\n"
        "press y to save it as your own text (trust human); any other key cancels\n"
    )


def rewrite_instead(new):
    return lambda t: t.replace("instead: run `pytest tests` instead", f"instead: {new}")


def test_an_automated_edit_is_not_applied_without_a_y(home, user_home):
    a = seed(home)
    ctx = make_ctx(user_home, keys="eq", edit=rewrite_instead("drop the flag; pytest has none"))
    out = run(ctx, "inbox")[1]
    assert preview(a, "drop the flag; pytest has none") + "edit cancelled; nothing changed\n" in out
    assert out.endswith("inbox: 0 approved, 0 edited, 0 muted, 0 skipped; 1 waiting\n")
    lesson = store.get_lesson(connect(home), a)
    assert (lesson.revision, lesson.trust_class, lesson.text.instead) == (
        2,
        TrustClass.OWN_CODE,
        "run `pytest tests` instead",
    )


UNRAN = (
    "`pytest -x tests` never ran successfully on this machine; "
    "press y again to keep it, any other key cancels\n"
)


def test_an_edited_command_that_never_passed_needs_a_second_y(home, user_home):
    a = seed(home)
    edit = rewrite_instead("run `pytest -x tests` instead")
    out = run(make_ctx(user_home, keys="eynq", edit=edit), "inbox")[1]
    assert preview(a, "run `pytest -x tests` instead") + UNRAN + "edit cancelled; nothing changed\n" in out
    assert store.get_lesson(connect(home), a).trust_class is TrustClass.OWN_CODE
    out = run(make_ctx(user_home, keys="eyyq", edit=edit), "inbox")[1]
    assert (
        preview(a, "run `pytest -x tests` instead") + UNRAN + f"edited {a}; it is a candidate again\n" in out
    )
    lesson = store.get_lesson(connect(home), a)
    assert (lesson.trust_class, lesson.text.instead) == (TrustClass.HUMAN, "run `pytest -x tests` instead")


def test_an_edited_command_that_passed_needs_one_y(home, user_home):
    a = seed(home)
    edit = rewrite_instead("run `pytest tests` from the repo root instead")
    out = run(make_ctx(user_home, keys="eyq", edit=edit), "inbox")[1]
    assert "never ran successfully" not in out
    assert f"edited {a}; it is a candidate again\n" in out
    assert store.get_lesson(connect(home), a).trust_class is TrustClass.HUMAN


def test_an_edit_over_the_repair_budget_is_refused_before_the_preview(home, user_home):
    a = seed(home)
    instead = (
        "wait for the running build to finish and then start the next one, "
        "because a second build started now takes the lock and fails"
    )
    reason = (
        "two builds that share one derived data folder corrupt each other, and the second one "
        "fails with a lock error every time, even when the first one is almost done"
    )

    def editor(text):
        text = text.replace("instead: run `pytest tests` instead", f"instead: {instead}")
        return text.replace(f"reason: {render.MINED_REASON}", f"reason: {reason}")

    out = run(make_ctx(user_home, keys="eyq", edit=editor), "inbox")[1]
    assert max(len(instead), len(reason)) <= TEXT_CAP
    assert "not applied: the lesson is over the repair budget of 400 characters\n" in out
    assert "this is how agents will see the edited lesson" not in out
    lesson = store.get_lesson(connect(home), a)
    assert (lesson.revision, lesson.trust_class, lesson.text.instead) == (
        2,
        TrustClass.OWN_CODE,
        "run `pytest tests` instead",
    )


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
