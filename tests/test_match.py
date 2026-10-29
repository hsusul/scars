import time

import pytest

from scars.match import (
    AnyArgs,
    CommandPattern,
    ErrorPattern,
    ExactArg,
    InvalidTrigger,
    MatchContext,
    OneArg,
    PathPattern,
    Precision,
    PrefixArg,
    PromptPattern,
    RecordedCall,
    call_matches,
    command_matches,
    matches,
    parse_spec,
    precision,
    prompt_paths,
    repo_relative,
    select,
    trigger_key,
)
from scars.model import (
    ErrorSignature,
    Harness,
    HookEvent,
    HookEventKind,
    Lesson,
    LessonId,
    LessonText,
    Level,
    MachineScope,
    PathScope,
    RepoId,
    RepoScope,
    SessionId,
    SessionScope,
    Stage,
    Status,
    ToolKind,
    Trigger,
    TriggerKind,
    TrustClass,
)
from scars.shell import SimpleCommand, Unsupported, parse

ROOT = "/Volumes/990 Pro/OpenSource/app"
REPO = RepoId("repo-1")
CTX = MatchContext(repo_root=ROOT, repo_id=REPO)
REPO_SCOPE = RepoScope(REPO)


def event(kind=HookEventKind.PRE_TOOL, *, command=None, paths=(), prompt=None, cwd=ROOT, session="s1"):
    return HookEvent(
        harness=Harness.CLAUDE,
        harness_version="2.1.299",
        event=kind,
        session_id=SessionId(session),
        tool_use_id=None,
        cwd=cwd,
        tool_kind=ToolKind.SHELL if command is not None else None,
        command=command,
        paths=tuple(paths),
        error_text=None,
        prompt=prompt,
        model_id=None,
    )


def lesson(
    lid,
    *triggers,
    status=Status.PROMOTED,
    level=Level.NOTE,
    pinned=False,
    scope=REPO_SCOPE,
    seen=1,
):
    return Lesson(
        id=LessonId(lid),
        revision=1,
        status=status,
        level=level,
        pinned=pinned,
        scope=scope,
        text=LessonText("a", "b", "c"),
        trust_class=TrustClass.OWN_CODE,
        triggers=tuple(triggers),
        fingerprint=lid,
        sessions_seen=seen,
    )


def cmd_trigger(spec, stage=Stage.GATE):
    return Trigger(TriggerKind.COMMAND, spec, stage)


def runs(spec, command):
    return matches(cmd_trigger(spec), event(command=command), CTX)


# Trigger grammar


@pytest.mark.parametrize(
    ("kind", "spec", "expected"),
    [
        (TriggerKind.COMMAND, "xcodebuild", CommandPattern(ExactArg("xcodebuild"), (), ())),
        (
            TriggerKind.COMMAND,
            "xcodebuild -scheme * !-showsdks",
            CommandPattern(ExactArg("xcodebuild"), (ExactArg("-scheme"), OneArg()), ("-showsdks",)),
        ),
        (
            TriggerKind.COMMAND,
            "git* push ** --force*",
            CommandPattern(PrefixArg("git"), (ExactArg("push"), AnyArgs(), PrefixArg("--force")), ()),
        ),
        (TriggerKind.PATH, "Apps/**/*.PY", PathPattern(("apps", "**", "*.py"))),
        (TriggerKind.ERROR, "KeyError: 'name'", ErrorPattern(ErrorSignature("KeyError: 'name'"))),
        (TriggerKind.PROMPT, "deploy  staging", PromptPattern(frozenset({"deploy", "staging"}))),
    ],
)
def test_parse_spec_builds_typed_patterns(kind, spec, expected):
    assert parse_spec(kind, spec) == expected


@pytest.mark.parametrize(
    ("kind", "spec", "reason"),
    [
        (TriggerKind.COMMAND, "", "needs a program"),
        (TriggerKind.COMMAND, "*", "first token must be a program"),
        (TriggerKind.COMMAND, "** make", "first token must be a program"),
        (TriggerKind.COMMAND, "!make", "first token must be a program"),
        (TriggerKind.COMMAND, "/usr/bin/make", "cannot contain '/'"),
        (TriggerKind.COMMAND, "make !", "followed by one literal flag"),
        (TriggerKind.COMMAND, "make !-x*", "followed by one literal flag"),
        (TriggerKind.COMMAND, "make a*b", "allowed only as"),
        (TriggerKind.COMMAND, "make ***", "allowed only as"),
        (TriggerKind.PATH, "", "non-empty glob"),
        (TriggerKind.PATH, "/abs/*.py", "non-empty glob"),
        (TriggerKind.PATH, "a//b", "empty, '.' or '..'"),
        (TriggerKind.PATH, "../x", "empty, '.' or '..'"),
        (TriggerKind.PATH, "src/a**", "whole segment"),
        (TriggerKind.ERROR, "", "one non-empty line"),
        (TriggerKind.ERROR, " padded", "one non-empty line"),
        (TriggerKind.ERROR, "two\nlines", "one non-empty line"),
        (TriggerKind.PROMPT, "  ", "at least one keyword"),
        (TriggerKind.PROMPT, "Deploy", "one lowercase word"),
        (TriggerKind.PROMPT, "re-deploy", "one lowercase word"),
    ],
)
def test_parse_spec_rejects_invalid_specs(kind, spec, reason):
    with pytest.raises(InvalidTrigger, match=reason):
        parse_spec(kind, spec)


# Command triggers


@pytest.mark.parametrize(
    ("spec", "command", "expected"),
    [
        ("xcodebuild", "xcodebuild -scheme X", True),
        ("xcodebuild", "xcrun xcodebuild", False),
        ("xcodebuild", "/usr/bin/xcodebuild -showsdks", True),
        ("xcodebuild -scheme *", "xcodebuild -scheme App", True),
        ("xcodebuild -scheme *", "xcodebuild -workspace W -scheme App -sdk ios", False),
        ("xcodebuild ** -scheme *", "xcodebuild -workspace W -scheme App -sdk ios", True),
        ("xcodebuild -scheme *", "xcodebuild -scheme", False),
        ("xcodebuild !-showsdks", "xcodebuild -scheme X", True),
        ("xcodebuild !-showsdks", "xcodebuild -showsdks", False),
        ("xcodebuild !-showsdks", "xcodebuild -scheme X -showsdks", False),
        ("git commit --amend", "git commit --amend", True),
        ("git commit --amend", "git commit -m x --amend", False),
        ("git commit ** --amend", "git commit -m x --amend", True),
        ("git commit ** --amend", "git commit --amend", True),
        ("git push --force", "git --force push", False),
        ("git push --force*", "git push --force-with-lease", True),
        ("git push --force*", "git push --forc", False),
        ("npm* test", "npm-run-all test", True),
        ("pytest -k", "pytest -k x", True),
        ("pytest -k", "pytest -kx", False),
        ("make * *", "make a", False),
        ("make * *", "make a b", True),
        ("make * x", "make a b x", False),
        ("make * x", "make a x", True),
        ("make ** x", "make a b x", True),
        ("make * x", "make x", False),
        ("git a * ** b", "git a x a y b", True),
        ("git a b ** c", "git c a b", False),
        ("make -j* ** --keep", "make --keep -j4", False),
        ("make -j* ** --keep", "make -j4 -s --keep", True),
        ("make -j* -s", "make -j4 x -j8 -s", False),
        ("make ** -j* -s", "make -j4 x -j8 -s", True),
        ("npm test", "cd apps/web && npm test", True),
        ("xcodebuild", "make && bash -c 'xcodebuild -scheme X'", True),
        ("xcodebuild", "echo 'a && xcodebuild'", False),
        ("xcodebuild", "$(xcodebuild)", False),
        ("cat", "cat <<EOF\nx\nEOF", False),
        ("pytest", "uv run pytest -k x", True),
        ("pytest", "env -u FOO TZ=UTC python3 -m pytest", True),
    ],
)
def test_command_trigger(spec, command, expected):
    assert runs(spec, command) is expected


@pytest.mark.parametrize(
    "command",
    [
        "echo ${x:-a; xcodebuild; b}",
        "xcodebuild/",
        "*/xcodebuild",
        "echo\x00; xcodebuild",
        "\ud800 xcodebuild",
    ],
    ids=["separator-in-parameter-expansion", "trailing-slash", "glob-dir", "nul", "lone-surrogate"],
)
def test_command_trigger_does_not_fire_on_round_two_findings(command):
    assert runs("xcodebuild", command) is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd ios && xcodebuild -scheme App", True),
        ("env -i xcodebuild", True),
        ("sudo -u ci xcodebuild", True),
        ("bash -c 'cd ios && xcodebuild'", True),
        ("/usr/bin/xcodebuild -showsdks", True),
        ("time nohup xcodebuild", True),
        ("~/bin/xcodebuild", True),
        ("echo $HOME; xcodebuild", True),
        ("x{a,b}; xcodebuild", False),
        ("env $X xcodebuild", False),
        ('bash -c "echo $X; xcodebuild"', False),
    ],
)
def test_command_trigger_fires_only_on_allowlisted_program_words(command, expected):
    assert runs("xcodebuild", command) is expected


def test_command_trigger_needs_a_command():
    assert matches(cmd_trigger("make"), event(paths=("Makefile",)), CTX) is False


# Path triggers


@pytest.mark.parametrize(
    ("spec", "path", "expected"),
    [
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/y/a.py", True),
        ("**/Package.swift", "Package.swift", True),
        ("**/Package.swift", "ios/app/package.SWIFT", True),
        ("SRC/*.PY", "src/a.py", True),
        ("src/**", "src/a/b", True),
        ("src/**", "lib/a", False),
        ("*.md", "notes/a.md", False),
        ("src/?.py", "src/ab.py", False),
        ("src/[ab].py", "src/b.py", True),
        ("src/a.py", f"{ROOT}/src/a.py", True),
        ("src/a.py", f"{ROOT.upper()}/SRC/A.PY", True),
        ("src/a.py", "/elsewhere/src/a.py", False),
        ("src/a.py", "../app/src/a.py", True),
    ],
)
def test_path_trigger(spec, path, expected):
    trigger = Trigger(TriggerKind.PATH, spec, Stage.GATE)
    assert matches(trigger, event(paths=(path,)), CTX) is expected


def test_path_trigger_resolves_relative_paths_against_cwd():
    trigger = Trigger(TriggerKind.PATH, "apps/web/*.ts", Stage.GATE)
    assert matches(trigger, event(paths=("index.ts",), cwd=f"{ROOT}/apps/web"), CTX) is True
    assert matches(trigger, event(paths=("index.ts",), cwd=ROOT), CTX) is False


def test_path_trigger_without_repo_root_uses_relative_paths_only():
    trigger = Trigger(TriggerKind.PATH, "src/*.py", Stage.GATE)
    assert matches(trigger, event(paths=("src/a.py",)), MatchContext()) is True
    assert matches(trigger, event(paths=(f"{ROOT}/src/a.py",)), MatchContext()) is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{ROOT}/Src/A.py", "src/a.py"),
        (f"{ROOT}/./src/../lib/x", "lib/x"),
        (ROOT, None),
        (f"{ROOT}-wt/src/a.py", None),
        ("../../x", None),
    ],
)
def test_repo_relative(path, expected):
    assert repo_relative(path, f"{ROOT}/src", ROOT) == expected


def test_prompt_paths_picks_path_like_words():
    prompt = "Fix `src/app.py`, then README.md. See https://x.io/a and the docs."
    assert prompt_paths(prompt) == ("src/app.py", "README.md")


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Please fix `src/app.py`.", ("src/app.py",)),
        ('Open "README.md".', ("README.md",)),
        ("(see `a/b.py`),", ("a/b.py",)),
        ("Is it 'lib/x.ts'?)", ("lib/x.ts",)),
        ("edit src/app.py...", ("src/app.py",)),
    ],
)
def test_prompt_paths_strips_closing_delimiters_and_sentence_punctuation(prompt, expected):
    assert prompt_paths(prompt) == expected


def test_path_trigger_reads_paths_named_in_a_prompt():
    trigger = Trigger(TriggerKind.PATH, "src/*.py", Stage.PLAN)
    assert matches(trigger, event(HookEventKind.PROMPT, prompt="edit src/app.py please"), CTX) is True
    assert matches(trigger, event(HookEventKind.PRE_TOOL, prompt="edit src/app.py"), CTX) is False


# Error and prompt triggers


def test_error_trigger_compares_the_precomputed_signature():
    trigger = Trigger(TriggerKind.ERROR, "KeyError: 'name'", Stage.REPAIR)
    failed = event(HookEventKind.TOOL_FAILED)
    assert matches(trigger, failed, MatchContext(error_signature=ErrorSignature("KeyError: 'name'"))) is True
    assert matches(trigger, failed, MatchContext(error_signature=ErrorSignature("KeyError: 'nam'"))) is False
    assert matches(trigger, failed, MatchContext()) is False


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Please DEPLOY to staging now", True),
        ("staging: deploy!", True),
        ("deploy to production", False),
        ("redeploy staging", False),
        ("deployment on staging", False),
    ],
)
def test_prompt_trigger_needs_every_keyword_as_a_whole_word(prompt, expected):
    trigger = Trigger(TriggerKind.PROMPT, "deploy staging", Stage.PLAN)
    assert matches(trigger, event(HookEventKind.PROMPT, prompt=prompt), CTX) is expected


def test_prompt_trigger_without_prompt_never_fires():
    assert matches(Trigger(TriggerKind.PROMPT, "x", Stage.PLAN), event(HookEventKind.PROMPT), CTX) is False


def test_matches_raises_on_a_malformed_spec():
    with pytest.raises(InvalidTrigger):
        matches(cmd_trigger("make a*b"), event(command="make"), CTX)


# Stage selection


def ids(selection):
    return [le.id for le in selection.lessons]


def test_plan_start_selects_promoted_pinned_in_scope_ranked():
    lessons = [
        lesson("L-c", pinned=True, seen=2),
        lesson("L-a", pinned=True, seen=5),
        lesson("L-b", pinned=True, seen=2, scope=MachineScope()),
        lesson("L-unpinned"),
        lesson("L-other-repo", pinned=True, scope=RepoScope(RepoId("other"))),
        lesson("L-path", pinned=True, scope=PathScope(REPO, "src/**")),
        lesson("L-prov", pinned=True, status=Status.PROVISIONAL, seen=9),
        lesson("L-cand", pinned=True, status=Status.CANDIDATE, seen=9),
    ]
    for kind in (HookEventKind.SESSION_START, HookEventKind.COMPACT):
        result = select(lessons, event(kind), CTX)
        assert ids(result) == ["L-a", "L-b", "L-c"]
        assert result.deadline_hit is False


def test_plan_start_without_repo_keeps_machine_scope_only():
    lessons = [lesson("L-repo", pinned=True), lesson("L-machine", pinned=True, scope=MachineScope())]
    assert ids(select(lessons, event(HookEventKind.SESSION_START), MatchContext())) == ["L-machine"]


def test_plan_prompt_needs_pinned_and_a_plan_stage_prompt_or_path_trigger_capped_at_2():
    prompt_trigger = Trigger(TriggerKind.PROMPT, "deploy", Stage.PLAN)
    lessons = [
        lesson("L-1", prompt_trigger, pinned=True),
        lesson("L-2", Trigger(TriggerKind.PATH, "src/*.py", Stage.PLAN), pinned=True),
        lesson("L-3", prompt_trigger, pinned=True),
        lesson("L-unpinned", prompt_trigger, seen=9),
        lesson("L-gate-stage", Trigger(TriggerKind.PROMPT, "deploy", Stage.GATE), pinned=True, seen=9),
        lesson("L-command", Trigger(TriggerKind.COMMAND, "deploy", Stage.PLAN), pinned=True, seen=9),
        lesson("L-prov", prompt_trigger, pinned=True, status=Status.PROVISIONAL, seen=9),
        lesson("L-cand", prompt_trigger, pinned=True, status=Status.CANDIDATE, seen=9),
        lesson(
            "L-cand-session",
            prompt_trigger,
            pinned=True,
            status=Status.CANDIDATE,
            scope=SessionScope(SessionId("s1")),
        ),
    ]
    result = select(lessons, event(HookEventKind.PROMPT, prompt="deploy src/a.py"), CTX)
    assert ids(result) == ["L-1", "L-2"]


def test_gate_needs_promoted_block_and_a_gate_command_or_path_trigger_capped_at_1():
    trig = cmd_trigger("xcodebuild")
    lessons = [
        lesson("L-warn", trig, level=Level.WARN, seen=9),
        lesson("L-block-b", trig, level=Level.BLOCK),
        lesson("L-block-a", trig, level=Level.BLOCK),
        lesson("L-plan", cmd_trigger("xcodebuild", Stage.PLAN), level=Level.BLOCK, seen=9),
        lesson("L-path", Trigger(TriggerKind.PATH, "*.xcodeproj/**", Stage.GATE), level=Level.BLOCK, seen=9),
        lesson("L-prov", trig, level=Level.BLOCK, status=Status.PROVISIONAL, seen=9),
        lesson("L-cand", trig, level=Level.BLOCK, status=Status.CANDIDATE, seen=9),
        lesson(
            "L-cand-session",
            trig,
            level=Level.BLOCK,
            status=Status.CANDIDATE,
            scope=SessionScope(SessionId("s1")),
        ),
    ]
    assert ids(select(lessons, event(command="xcodebuild -scheme X"), CTX)) == ["L-block-a"]
    assert ids(select(lessons, event(command="$(xcodebuild)"), CTX)) == []


def test_gate_path_trigger_fires_on_real_edit_paths():
    gate = lesson(
        "L-pbx", Trigger(TriggerKind.PATH, "*.xcodeproj/project.pbxproj", Stage.GATE), level=Level.BLOCK
    )
    edit = event(paths=(f"{ROOT}/App.xcodeproj/project.pbxproj",))
    assert ids(select([gate], edit, CTX)) == ["L-pbx"]
    assert ids(select([gate], event(paths=("App.xcodeproj/project.pbxproj",)), CTX)) == ["L-pbx"]
    assert ids(select([gate], event(paths=(f"{ROOT}/ios/App.xcodeproj/project.pbxproj",)), CTX)) == []
    assert ids(select([gate], event(paths=(f"{ROOT}/App.xcodeproj/xcuserdata",)), CTX)) == []


def test_repair_fires_only_error_triggers():
    ctx = MatchContext(repo_root=ROOT, repo_id=REPO, error_signature=ErrorSignature("boom"))
    failed = HookEvent(
        harness=Harness.CLAUDE,
        harness_version="2.1.299",
        event=HookEventKind.TOOL_FAILED,
        session_id=SessionId("s1"),
        tool_use_id=None,
        cwd=ROOT,
        tool_kind=ToolKind.SHELL,
        command="xcodebuild -scheme X",
        paths=(f"{ROOT}/src/a.py",),
        error_text="boom",
        prompt="deploy",
        model_id=None,
    )
    lessons = [
        lesson("L-cmd", Trigger(TriggerKind.COMMAND, "xcodebuild", Stage.REPAIR), seen=9),
        lesson("L-path", Trigger(TriggerKind.PATH, "src/*.py", Stage.REPAIR), seen=9),
        lesson("L-prompt", Trigger(TriggerKind.PROMPT, "deploy", Stage.REPAIR), seen=9),
        lesson("L-err", Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR)),
    ]
    assert ids(select(lessons, failed, ctx)) == ["L-err"]


def test_repair_error_trigger_fires_when_the_command_is_unsupported():
    ctx = MatchContext(repo_root=ROOT, repo_id=REPO, error_signature=ErrorSignature("boom"))
    err = lesson("L-err", Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR))
    assert ids(select([err], event(HookEventKind.TOOL_FAILED, command="$(xcodebuild)"), ctx)) == ["L-err"]


def test_repair_ranks_promoted_then_provisional_and_admits_this_sessions_candidates():
    err = Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR)
    ctx = MatchContext(repo_root=ROOT, repo_id=REPO, error_signature=ErrorSignature("boom"))
    lessons = [
        lesson("L-cand", err, status=Status.CANDIDATE, scope=SessionScope(SessionId("s1")), seen=1),
        lesson("L-prov", err, status=Status.PROVISIONAL, seen=9),
        lesson("L-cand-repo", err, status=Status.CANDIDATE, seen=9),
        lesson("L-cand-other", err, status=Status.CANDIDATE, scope=SessionScope(SessionId("s2")), seen=9),
        lesson("L-retired", err, status=Status.RETIRED, seen=9),
    ]
    failed = event(HookEventKind.TOOL_FAILED)
    assert ids(select(lessons, failed, ctx)) == ["L-prov", "L-cand"]
    assert ids(select([*lessons, lesson("L-prom", err, seen=9)], failed, ctx)) == ["L-prom", "L-prov"]
    assert ids(select([*lessons, lesson("L-prom", err, seen=1)], failed, ctx)) == ["L-prov", "L-prom"]


def test_path_scope_glob_must_match_an_event_path():
    scoped = lesson(
        "L-ios",
        Trigger(TriggerKind.PATH, "**/*.py", Stage.GATE),
        level=Level.BLOCK,
        scope=PathScope(REPO, "ios/**"),
    )
    assert ids(select([scoped], event(paths=(f"{ROOT}/web/app.py",)), CTX)) == []
    assert ids(select([scoped], event(paths=(f"{ROOT}/ios/app.py",)), CTX)) == ["L-ios"]


def test_path_scope_is_not_admitted_at_the_repo_root_without_a_path_inside_the_glob():
    gate = lesson("L-gate", cmd_trigger("xcodebuild"), level=Level.BLOCK, scope=PathScope(REPO, "ios/**"))
    assert ids(select([gate], event(command="xcodebuild"), CTX)) == []
    plan = lesson(
        "L-plan",
        Trigger(TriggerKind.PROMPT, "deploy", Stage.PLAN),
        pinned=True,
        scope=PathScope(REPO, "ios/**"),
    )
    assert ids(select([plan], event(HookEventKind.PROMPT, prompt="deploy now"), CTX)) == []
    assert ids(select([plan], event(HookEventKind.PROMPT, prompt="deploy ios/app.swift"), CTX)) == ["L-plan"]


def test_plan_start_admits_only_machine_and_repo_scope():
    lessons = [
        lesson("L-machine", pinned=True, scope=MachineScope()),
        lesson("L-repo", pinned=True),
        lesson("L-path", pinned=True, scope=PathScope(REPO, "**")),
        lesson("L-session", pinned=True, scope=SessionScope(SessionId("s1"))),
    ]
    for kind in (HookEventKind.SESSION_START, HookEventKind.COMPACT):
        assert ids(select(lessons, event(kind, paths=(f"{ROOT}/a.py",)), CTX)) == ["L-machine", "L-repo"]


def test_select_reports_the_firing_trigger_and_its_key():
    fired = cmd_trigger("xcodebuild -scheme *")
    gate = lesson("L-1", cmd_trigger("make"), fired, level=Level.BLOCK)
    result = select([gate], event(command="xcodebuild -scheme X"), CTX)
    assert [(s.lesson.id, s.trigger, s.trigger_key) for s in result.selected] == [
        ("L-1", fired, "command:gate:xcodebuild -scheme *")
    ]
    start = select([lesson("L-2", pinned=True)], event(HookEventKind.SESSION_START), CTX)
    assert [(s.lesson.id, s.trigger, s.trigger_key) for s in start.selected] == [("L-2", None, "start:plan")]


def test_select_skips_already_shown_pairs_before_the_cap():
    prompt_trigger = Trigger(TriggerKind.PROMPT, "deploy", Stage.PLAN)
    lessons = [lesson(f"L-{i}", prompt_trigger, pinned=True, seen=9 - i) for i in range(3)]
    shown = {(LessonId("L-0"), "prompt:plan:deploy"), (LessonId("L-1"), "prompt:plan:other")}
    result = select(lessons, event(HookEventKind.PROMPT, prompt="deploy"), CTX, shown=shown)
    assert ids(result) == ["L-1", "L-2"]


def test_select_tries_another_trigger_when_the_first_was_shown():
    keyword, path = (
        Trigger(TriggerKind.PROMPT, "deploy", Stage.PLAN),
        Trigger(TriggerKind.PATH, "*.py", Stage.PLAN),
    )
    plan = lesson("L-0", keyword, path, pinned=True)
    prompt = event(HookEventKind.PROMPT, prompt="deploy a.py")
    result = select([plan], prompt, CTX, shown={(LessonId("L-0"), "prompt:plan:deploy")})
    assert [s.trigger_key for s in result.selected] == ["path:plan:*.py"]
    assert (
        select([plan], prompt, CTX, shown={("L-0", "prompt:plan:deploy"), ("L-0", "path:plan:*.py")}).selected
        == ()
    )


def test_gate_never_deduplicates_a_deny():
    gate = lesson("L-0", cmd_trigger("xcodebuild"), level=Level.BLOCK)
    shown = {(LessonId("L-0"), "command:gate:xcodebuild")}
    assert ids(select([gate], event(command="xcodebuild"), CTX, shown=shown)) == ["L-0"]


def test_select_reports_a_deadline_passed_during_the_last_evaluation(monkeypatch):
    import scars.match as match_module

    now = [0.0]
    real = match_module.matches

    def slow(*args, **kwargs):
        now[0] += 0.060
        return real(*args, **kwargs)

    monkeypatch.setattr(match_module, "matches", slow)
    gate = lesson("L-0", cmd_trigger("xcodebuild"), level=Level.BLOCK)
    result = select([gate], event(command="xcodebuild"), CTX, clock=lambda: now[0])
    assert ids(result) == ["L-0"]
    assert result.deadline_hit is True


def test_select_skips_a_malformed_trigger_and_keeps_the_rest():
    lessons = [
        lesson("L-bad", cmd_trigger("make a*b"), level=Level.BLOCK),
        lesson("L-good", cmd_trigger("make"), level=Level.BLOCK),
    ]
    assert ids(select(lessons, event(command="make"), CTX)) == ["L-good"]


def test_select_stops_at_the_deadline_with_matches_so_far():
    # One reading to set the deadline, then one after each lesson.
    ticks = iter([0.0, 0.010, 0.051])
    lessons = [lesson(f"L-{i}", pinned=True) for i in range(4)]
    result = select(lessons, event(HookEventKind.SESSION_START), CTX, clock=lambda: next(ticks))
    assert ids(result) == ["L-0", "L-1"]
    assert result.deadline_hit is True


def test_thousand_command_lessons_against_a_4_kb_command_finish_well_inside_the_deadline():
    command = "xcodebuild -scheme App " + " ".join(f"-arg{i}" for i in range(600))
    command = command[: 4096 - 20] + " && make"
    assert 4000 < len(command) <= 4096
    # Worst case: every token of every lesson occurs in the command, so the set and prefix pre-checks pass,
    # but `-arg0` never follows `-argN*`, so each lesson's `**` scans all ~600 arguments.
    lessons = [
        lesson(f"L-{i:04}", cmd_trigger(f"xcodebuild ** -arg{i % 500 + 1}* -arg0 !-no{i}"), level=Level.BLOCK)
        for i in range(999)
    ]
    # Ranks last, so the scan visits every other lesson before the gate cap stops it.
    lessons.append(lesson("L-zzzz", cmd_trigger("make"), level=Level.BLOCK))
    gate = event(command=command)
    select(lessons[:10], gate, CTX, deadline_ms=10_000)
    start = time.perf_counter()
    result = select(lessons, gate, CTX, deadline_ms=10_000)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"1,000 command lessons against a {len(command)}-byte command: {elapsed_ms:.1f} ms")
    assert ids(result) == ["L-zzzz"]
    assert result.deadline_hit is False
    assert elapsed_ms < 1000


# Precision check


def call(command, *, ok, sig=None):
    return RecordedCall(ToolKind.SHELL, command, (), ok, ErrorSignature(sig) if sig else None)


def test_precision_counts_failing_and_passing_matches():
    sig = ErrorSignature("boom")
    calls = [
        call("xcodebuild -scheme A", ok=False, sig="boom"),
        call("xcodebuild -scheme B", ok=False, sig="boom"),
        call("xcodebuild -scheme C", ok=False, sig="other"),
        call("xcodebuild -scheme D", ok=True),
        call("make", ok=False, sig="boom"),
        call("$(xcodebuild)", ok=True),
    ]
    result = precision(cmd_trigger("xcodebuild"), calls, error_signature=sig)
    assert result == Precision(matched_failing=2, matched_passing=1, matched=4, total=6)


def test_precision_replays_path_triggers_against_recorded_paths():
    calls = [
        RecordedCall(ToolKind.EDIT, None, (f"{ROOT}/src/a.py",), False, ErrorSignature("boom")),
        RecordedCall(ToolKind.EDIT, None, ("src/b.py",), True, None, cwd=ROOT),
        RecordedCall(ToolKind.EDIT, None, ("lib/c.py",), True, None, cwd=ROOT),
    ]
    trigger = Trigger(TriggerKind.PATH, "src/*.py", Stage.GATE)
    result = precision(trigger, calls, error_signature=ErrorSignature("boom"), repo_root=ROOT)
    assert result == Precision(1, 1, 2, 3)


def test_precision_resolves_paths_against_each_calls_repo_root():
    sig = ErrorSignature("boom")
    calls = [
        RecordedCall(ToolKind.EDIT, None, ("/wt1/src/a.py",), False, sig, repo_root="/wt1"),
        RecordedCall(ToolKind.EDIT, None, ("/wt2/src/b.py",), True, None, repo_root="/wt2"),
        RecordedCall(ToolKind.EDIT, None, ("/wt2/src/c.py",), True, None),
    ]
    trigger = Trigger(TriggerKind.PATH, "src/*.py", Stage.GATE)
    assert precision(trigger, calls, error_signature=sig, repo_root="/wt1") == Precision(1, 1, 2, 3)


def test_precision_respects_the_calls_tool_kind():
    sig = ErrorSignature("boom")
    calls = [
        RecordedCall(ToolKind.EDIT, "xcodebuild", (), False, sig),
        RecordedCall(ToolKind.OTHER, "xcodebuild", (), True, None),
        RecordedCall(ToolKind.SHELL, "xcodebuild", (), False, sig),
    ]
    assert precision(cmd_trigger("xcodebuild"), calls, error_signature=sig) == Precision(1, 0, 1, 3)


@pytest.mark.parametrize(
    ("trigger", "recorded", "expected"),
    [
        (cmd_trigger("xcodebuild"), RecordedCall(ToolKind.SHELL, "xcodebuild -x", (), True, None), True),
        (cmd_trigger("xcodebuild"), RecordedCall(ToolKind.EDIT, "xcodebuild", ("a",), True, None), False),
        (cmd_trigger("xcodebuild"), RecordedCall(ToolKind.SHELL, "$(xcodebuild)", (), True, None), False),
        (
            Trigger(TriggerKind.PATH, "src/*.py", Stage.GATE),
            RecordedCall(ToolKind.EDIT, None, ("/wt2/src/a.py",), True, None, repo_root="/wt2"),
            True,
        ),
        (
            Trigger(TriggerKind.PATH, "src/*.py", Stage.GATE),
            RecordedCall(ToolKind.EDIT, None, ("/wt2/src/a.py",), True, None),
            False,
        ),
        (
            Trigger(TriggerKind.PATH, "**", Stage.GATE),
            RecordedCall(ToolKind.SHELL, "ls", (), True, None),
            False,
        ),
        (
            Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR),
            RecordedCall(ToolKind.SHELL, "x", (), False, ErrorSignature("boom")),
            True,
        ),
        (
            Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR),
            RecordedCall(ToolKind.SHELL, "x", (), True, None),
            False,
        ),
    ],
)
def test_call_matches(trigger, recorded, expected):
    assert call_matches(trigger, recorded, repo_root="/wt1") is expected


def test_call_matches_rejects_prompt_triggers():
    with pytest.raises(ValueError, match="prompt"):
        call_matches(
            Trigger(TriggerKind.PROMPT, "x", Stage.PLAN), RecordedCall(ToolKind.SHELL, "x", (), True, None)
        )


def test_trigger_key():
    assert trigger_key(Trigger(TriggerKind.PATH, "src/**", Stage.GATE)) == "path:gate:src/**"


def test_precision_rejects_non_pre_triggers():
    with pytest.raises(ValueError, match="command and path"):
        precision(
            Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR), [], error_signature=ErrorSignature("boom")
        )


@pytest.mark.parametrize(
    ("failing", "passing", "keeps"),
    [(0, 0, True), (1, 4, True), (1, 5, False), (0, 1, False), (3, 12, True), (3, 13, False)],
)
def test_keep_pre_trigger_rule(failing, passing, keeps):
    assert Precision(failing, passing, failing + passing, 100).keeps_pre_trigger is keeps


@pytest.mark.parametrize(
    ("failing", "passing", "allowed"),
    [(3, 0, True), (2, 0, False), (99, 1, False), (100, 1, True), (297, 3, False), (298, 3, True)],
)
def test_block_rule(failing, passing, allowed):
    assert Precision(failing, passing, failing + passing, 1000).allows_block is allowed


@pytest.mark.parametrize(
    ("matched", "total", "broad"),
    [(0, 0, False), (5, 100, False), (6, 100, True), (1, 20, False), (1, 19, True), (1, 1, True)],
)
def test_broad_trigger_warning_is_above_5_percent_of_all_calls(matched, total, broad):
    assert Precision(0, 0, matched, total).warns_broad is broad


def test_precision_reports_a_broad_trigger_end_to_end():
    sig = ErrorSignature("boom")
    hit = call("xcodebuild", ok=True)
    assert precision(
        cmd_trigger("xcodebuild"), [hit] + [call("make", ok=True)] * 19, error_signature=sig
    ) == (Precision(0, 1, 1, 20))
    assert precision(
        cmd_trigger("xcodebuild"), [hit] * 2 + [call("make", ok=True)] * 18, error_signature=sig
    ).warns_broad
    assert not precision(
        cmd_trigger("xcodebuild"), [hit] + [call("make", ok=True)] * 19, error_signature=sig
    ).warns_broad
    assert precision(cmd_trigger("xcodebuild"), [], error_signature=sig).warns_broad is False


# Every matcher entry point agrees


def entry_points(spec, command):
    """What each public matcher says about one command trigger and one shell command."""
    trigger = cmd_trigger(spec)
    pattern = parse_spec(TriggerKind.COMMAND, spec)
    try:
        simple = parse(command)
    except Unsupported:
        simple = ()
    recorded = RecordedCall(ToolKind.SHELL, command, (), True, None)
    gate = lesson("L-gate", trigger, level=Level.BLOCK)
    return {
        "matches": matches(trigger, event(command=command), CTX),
        "command_matches": any(command_matches(pattern, c) for c in simple),
        "call_matches": call_matches(trigger, recorded),
        "precision": precision(trigger, [recorded], error_signature=ErrorSignature("x")).matched == 1,
        "select": ids(select([gate], event(command=command), CTX)) == ["L-gate"],
    }


def assert_everywhere(spec, command, expected):
    assert entry_points(spec, command) == dict.fromkeys(
        ("matches", "command_matches", "call_matches", "precision", "select"), expected
    )


@pytest.mark.parametrize(
    ("spec", "command", "expected"),
    [
        ("git push --force", "git push --force", True),
        ("git push --force", "git push --force origin", True),
        ("git push --force", "git log -- push --force", False),
        ("git push --force", "git log push --force", False),
        ("git push --force", "git -C x push --force", False),
        ("git ** push --force", "git -C x push --force", True),
        ("git ** push --force", "git log -- push --force", False),
        ("git push * --force", "git push origin --force", True),
        ("git push * --force", "git push --force", False),
        ("xcodebuild -scheme *", "xcodebuild -workspace W -scheme X", False),
        ("xcodebuild ** -scheme *", "xcodebuild -workspace W -scheme X", True),
        ("xcodebuild -scheme *", "xcodebuild -scheme X -sdk Y", True),
        ("git checkout -- *", "git checkout -- a.txt", True),
        ("git checkout * a.txt", "git checkout -- a.txt", False),
        ("git checkout ** --force", "git checkout -- --force", False),
        ("git checkout ** -- --force", "git checkout x -- --force", True),
        ("git checkout ** -- --force", "git checkout -- x -- --force", False),
        ("rm -- -rf", "rm -- -rf", True),
        ("rm !-f", "rm -- -f", True),
        ("rm !-f", "rm -f -- x", False),
        ("rm !-f", "rm x -f", False),
        ("make ** --keep", "make --keep", True),
    ],
)
def test_argument_tokens_anchor_at_the_first_argument_and_stop_at_double_dash(spec, command, expected):
    assert_everywhere(spec, command, expected)


@pytest.mark.parametrize(
    ("spec", "command", "expected"),
    [
        ("xcodebuild !-showsdks", "xcodebuild $FLAGS", False),
        ("xcodebuild !-showsdks", 'xcodebuild "$FLAGS"', False),
        ("xcodebuild !-showsdks", "xcodebuild -scheme X *", False),
        ("xcodebuild !-showsdks", "xcodebuild -- $FLAGS", True),
        ("xcodebuild !-showsdks", "xcodebuild -scheme X", True),
        ("git push $REMOTE", "git push $REMOTE", False),
        ("cat ~/x", "cat ~/x", False),
        ("rm -r*", "rm -r[f] x", False),
        ("rm -rf *", "rm -rf $DIR", False),
        ("git ** --force", "git $X --force", False),
        ("git ** --force", 'git "$X" --force', False),
        ("git push --force", "git push --force $X", True),
        ("git push --force **", "git push --force *.txt", True),
        ("rm *", "rm '*'", True),
        ("xcodebuild", '"$CC"', False),
        ("xcodebuild", "$X -scheme", False),
        ("x*", "x* -a", False),
    ],
)
def test_unresolved_expansions_never_count_as_literal_arguments(spec, command, expected):
    assert_everywhere(spec, command, expected)


def test_command_matches_on_a_hand_built_command_with_uncertain_arguments():
    pattern = parse_spec(TriggerKind.COMMAND, "xcodebuild !-showsdks")
    assert command_matches(pattern, SimpleCommand("xcodebuild", ("-scheme", "X"), None)) is True
    uncertain = SimpleCommand("xcodebuild", ("-scheme", "$S"), None, frozenset({1}))
    assert command_matches(pattern, uncertain) is False


# Path scope admission


def ios_gate(trigger=None):
    return lesson(
        "L-ios", trigger or cmd_trigger("xcodebuild"), level=Level.BLOCK, scope=PathScope(REPO, "ios/**")
    )


@pytest.mark.parametrize(
    ("command", "paths", "cwd", "context", "admitted"),
    [
        ("cd ios && xcodebuild", (), ROOT, CTX, True),
        ("cd web && xcodebuild", (), ROOT, CTX, False),
        ("bash -c 'cd ios && xcodebuild'", (), ROOT, CTX, True),
        ("cd ios && make; xcodebuild", (), ROOT, CTX, False),
        ("make; cd ios; xcodebuild", (), ROOT, CTX, False),
        ("xcodebuild", (), f"{ROOT}/ios", CTX, True),
        ("xcodebuild", (), f"{ROOT}/ios/App", CTX, True),
        ("xcodebuild", (), f"{ROOT}/web", CTX, False),
        ("xcodebuild", (), ROOT, CTX, False),
        ("xcodebuild", (), "", CTX, False),
        ("xcodebuild", (f"{ROOT}/ios/a.swift",), "/elsewhere", CTX, True),
        ("xcodebuild", ("a.swift",), f"{ROOT}/ios", CTX, True),
        ("xcodebuild", ("ios/a.swift",), f"{ROOT}/web", CTX, False),
        ("xcodebuild", ("ios/a.swift",), "", CTX, False),
        ("xcodebuild", ("ios/a.swift",), ROOT, MatchContext(repo_id=REPO), False),
        ("cd ios && xcodebuild", (), ROOT, MatchContext(repo_id=REPO), False),
        ("xcodebuild", ("ios/a.swift",), ROOT, MatchContext(repo_root=ROOT), False),
    ],
)
def test_path_scope_needs_a_repo_root_and_a_location_inside_the_glob(command, paths, cwd, context, admitted):
    gate = event(command=command, paths=paths, cwd=cwd)
    assert ids(select([ios_gate()], gate, context)) == (["L-ios"] if admitted else [])


def test_path_scope_uses_the_directory_of_the_command_the_trigger_matched():
    gate = event(command="cd ios && make", cwd=ROOT)
    assert ids(select([ios_gate(cmd_trigger("make"))], gate, CTX)) == ["L-ios"]
    other = event(command="(cd ios && make) ; xcodebuild", cwd=ROOT)
    assert ids(select([ios_gate()], other, CTX)) == []


@pytest.mark.parametrize(
    "command",
    [
        "bash -o noexec -c 'git push --force'",
        "bash -n -c 'git push --force'",
        "sh -o noexec -c 'git push --force'",
        "git push --force < ''",
        "git push --force > ''",
        "git push --force > /",
        "cd -L && git push --force",
        "cd - && git push --force",
        "cd -L ios && git push --force",
        "sudo -u '' git push --force",
        "env -u '' git push --force",
        "uv run --with '' git push --force",
        "set -o noexec; git push --force",
        "exit; git push --force",
        "PATH=/nowhere git push --force",
        # Not unwrapped, so the program is the wrapper itself and nothing fires.
        "command git push --force",
        "command -v git push --force",
        "builtin git push --force",
        "nice -n 5 git push --force",
        "env '' git push --force",
        "nohup '' git push --force",
        "time '' git push --force",
    ],
)
def test_command_trigger_does_not_fire_on_round_four_findings(command):
    assert_everywhere("git push --force", command, False)


@pytest.mark.parametrize("command", ["cd -L && git push --force", "cd - && git push --force"])
def test_path_scope_is_not_admitted_from_a_cd_option(command):
    anywhere = lesson(
        "L-any", cmd_trigger("git push --force"), level=Level.BLOCK, scope=PathScope(REPO, "**")
    )
    assert ids(select([anywhere], event(command=command, cwd=ROOT), CTX)) == []
    assert ids(select([anywhere], event(command="cd sub && git push --force", cwd=ROOT), CTX)) == ["L-any"]


def test_path_scope_admits_a_plan_lesson_from_the_session_cwd():
    plan = lesson(
        "L-plan",
        Trigger(TriggerKind.PROMPT, "deploy", Stage.PLAN),
        pinned=True,
        scope=PathScope(REPO, "ios/**"),
    )
    prompt = event(HookEventKind.PROMPT, prompt="deploy now", cwd=f"{ROOT}/ios")
    assert ids(select([plan], prompt, CTX)) == ["L-plan"]
    assert ids(select([plan], prompt, MatchContext(repo_id=REPO))) == []


# Ranking by score


def test_select_ranks_by_score_then_status_then_id():
    err = Trigger(TriggerKind.ERROR, "boom", Stage.REPAIR)
    ctx = MatchContext(repo_root=ROOT, repo_id=REPO, error_signature=ErrorSignature("boom"))
    failed = event(HookEventKind.TOOL_FAILED)
    lessons = [
        lesson("L-a", err, seen=9),
        lesson("L-b", err, seen=1),
        lesson("L-c", err, seen=1, status=Status.PROVISIONAL),
        lesson("L-d", err, seen=1),
    ]
    assert ids(select(lessons, failed, ctx)) == ["L-a", "L-b"]
    assert ids(select(lessons, failed, ctx, scores={LessonId("L-d"): 2.0, LessonId("L-a"): 1.0})) == [
        "L-d",
        "L-a",
    ]
    assert ids(select(lessons, failed, ctx, scores={LessonId("L-c"): 0.5})) == ["L-c", "L-a"]
    assert ids(select(lessons, failed, ctx, scores={})) == ["L-a", "L-b"]
    assert ids(select(lessons[1:], failed, ctx, scores={})) == ["L-b", "L-d"]
