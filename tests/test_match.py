import time

import pytest

from scars.match import (
    STAGE_RULES,
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
    matches,
    parse_spec,
    precision,
    prompt_paths,
    repo_relative,
    select,
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
        ("xcodebuild -scheme *", "xcodebuild -workspace W -scheme App -sdk ios", True),
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
        ("make * x", "make a b x", True),
        ("make * x", "make x", False),
        ("git a * ** b", "git a x a y b", True),
        ("git a b ** c", "git c a b", False),
        ("make -j* ** --keep", "make --keep -j4", False),
        ("make -j* ** --keep", "make -j4 -s --keep", True),
        ("make -j* -s", "make -j4 x -j8 -s", True),
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
        ("*.md", "docs/a.md", False),
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


def test_stage_table_caps():
    assert {kind: rule.cap for kind, rule in STAGE_RULES.items()} == {
        HookEventKind.SESSION_START: None,
        HookEventKind.COMPACT: None,
        HookEventKind.PROMPT: 2,
        HookEventKind.PRE_TOOL: 1,
        HookEventKind.TOOL_FAILED: 2,
    }


def test_plan_start_selects_promoted_pinned_in_scope_ranked():
    lessons = [
        lesson("L-c", pinned=True, seen=2),
        lesson("L-a", pinned=True, seen=5),
        lesson("L-b", pinned=True, seen=2, scope=MachineScope()),
        lesson("L-unpinned"),
        lesson("L-other-repo", pinned=True, scope=RepoScope(RepoId("other"))),
        lesson("L-path", pinned=True, scope=PathScope(REPO, "src/**")),
    ]
    for kind in (HookEventKind.SESSION_START, HookEventKind.COMPACT):
        result = select(lessons, event(kind), CTX)
        assert ids(result) == ["L-a", "L-b", "L-c", "L-path"]
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
        lesson("L-path", Trigger(TriggerKind.PATH, "*.xcodeproj", Stage.GATE), level=Level.BLOCK, seen=9),
    ]
    assert ids(select(lessons, event(command="xcodebuild -scheme X"), CTX)) == ["L-block-a"]
    assert ids(select(lessons, event(command="$(xcodebuild)"), CTX)) == []


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
    assert ids(select([*lessons, lesson("L-prom", err)], failed, ctx)) == ["L-prom", "L-prov"]


def test_select_skips_a_malformed_trigger_and_keeps_the_rest():
    lessons = [
        lesson("L-bad", cmd_trigger("make a*b"), level=Level.BLOCK),
        lesson("L-good", cmd_trigger("make"), level=Level.BLOCK),
    ]
    assert ids(select(lessons, event(command="make"), CTX)) == ["L-good"]


def test_select_stops_at_the_deadline_with_matches_so_far():
    ticks = iter([0.0, 0.0, 0.010, 0.051, 0.052])
    lessons = [lesson(f"L-{i}", pinned=True) for i in range(4)]
    result = select(lessons, event(HookEventKind.SESSION_START), CTX, clock=lambda: next(ticks))
    assert ids(result) == ["L-0", "L-1"]
    assert result.deadline_hit is True


def test_thousand_command_lessons_against_a_4_kb_command_finish_well_inside_the_deadline():
    command = "xcodebuild -scheme App " + " ".join(f"-arg{i}" for i in range(600))
    command = command[: 4096 - 20] + " && make"
    assert 4000 < len(command) <= 4096
    # Worst case: every token of every lesson occurs in the command, so the set and prefix pre-checks pass,
    # but `-arg0` never follows `-argN*`, so each lesson scans all ~600 arguments.
    lessons = [
        lesson(f"L-{i:04}", cmd_trigger(f"xcodebuild -arg{i % 500 + 1}* -arg0 !-no{i}"), level=Level.BLOCK)
        for i in range(1000)
    ]
    start = time.perf_counter()
    result = select(lessons, event(command=command), CTX, deadline_ms=10_000)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"1,000 command lessons against a {len(command)}-byte command: {elapsed_ms:.1f} ms")
    assert result.lessons == ()
    assert result.deadline_hit is False
    assert elapsed_ms < 250


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
