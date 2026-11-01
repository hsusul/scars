import pytest

from scars.mine.calls import CallStatus, make_call
from scars.mine.pairs import IDLE_SECONDS, Target, pair_session, same_target, target
from scars.model import Harness

START = 1_800_000_000.0
FAIL = "pytest: error: unrecognized arguments: --frobnicate"


def call(tool_use_id, at, *, tool="Bash", command="pytest tests", status=CallStatus.OK, output=""):
    return make_call(
        harness=Harness.CLAUDE,
        session_id="s1",
        tool_use_id=tool_use_id,
        tool=tool,
        raw_input={"command": command},
        command=command if tool == "Bash" else None,
        paths=(),
        status=status,
        output=output,
        at=at,
        cwd="/tmp/app",
        model_id=None,
    )


def failed(tool_use_id, at, **kwargs):
    return call(tool_use_id, at, status=CallStatus.FAILED, output=FAIL, **kwargs)


def outcome(result):
    return (
        [(p.failed.tool_use_id, p.passing.tool_use_id) for p in result.pairs],
        [f.tool_use_id for f in result.still_open],
        result.failures,
        result.unlessonable,
        result.dropped,
    )


def test_a_failure_pairs_with_the_next_pass_of_the_same_program():
    result = pair_session(
        (), [failed("f", START, command="pytest --x tests"), call("p", START + 1)], START + 2
    )
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)


def test_a_failed_edit_call_never_opens():
    edit = failed("f", START, tool="Edit")
    result = pair_session((), [edit, call("r", START + 1, tool="Read")], START + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failed_shell_call_the_parser_rejects_never_opens():
    result = pair_session(
        (), [failed("f", START, command="pytest $("), call("p", START + 1, command="echo $(")], START + 2
    )
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_with_no_lessonable_signature_never_opens():
    timeout = call(
        "f", START, command="pytest --x", status=CallStatus.FAILED, output="Command timed out after 2m 0s"
    )
    result = pair_session((), [timeout, call("p", START + 1)], START + 2)
    assert outcome(result) == ([], [], 1, 1, 0)


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("cat CONTRIBUTING.md; rg missing .github", "# Contributing..."),
        ("cat AGENTS.md; rg missing src", "# Useful Commands"),
        ("sed -n '1,5p' config.py | rg missing", "import asyncio"),
        ("tail -n 100 audit.md && git status --short", "# Contribution chain run 2"),
        ("cat README.md || rg missing src", "# Individual Contributor License Agreement"),
    ],
)
def test_compound_stdout_without_an_error_never_opens_a_failure(command, output):
    result = pair_session(
        (), [call("f", START, command=command, status=CallStatus.FAILED, output=output)], START + 1
    )
    assert outcome(result) == ([], [], 1, 1, 0)


def test_a_failure_idle_for_a_day_is_dropped_by_the_next_call():
    late = call("p", START + IDLE_SECONDS)
    result = pair_session((), [failed("f", START, command="pytest --x"), late], START + IDLE_SECONDS)
    assert outcome(result) == ([], [], 1, 0, 1)


def test_a_pass_on_another_operand_is_skipped_and_the_failure_keeps_waiting():
    stream = [
        failed("f", START, command="git push origin feature"),
        call("other", START + 1, command="git push origin main"),
        call("fix", START + 2, command="git push --set-upstream origin feature"),
    ]
    result = pair_session((), stream, START + 3)
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


@pytest.mark.parametrize(
    ("command", "fix", "output"),
    [
        (
            "sed -n 1,280p lib/realtime/gen_rpc_pub_sub.ex",
            "sed -n 1,280p lib/realtime/gen_rpc/pub_sub.ex",
            "sed: lib/realtime/gen_rpc_pub_sub.ex: No such file or directory",
        ),
        ("cat missing.txt", "cat actual.txt", "cat: missing.txt: No such file or directory"),
        ("ls src/missing", "ls src/actual", "ls: cannot access 'src/missing': No such file or directory"),
        ("ls src/missing", "ls src/actual", "ls: cannot access 'src/missing'"),
        ("cat missing.txt", "cat actual.txt", "Error: File 'missing.txt' does not exist."),
        ("cat src/missing", "cat src/actual", "error: src/missing does not exist"),
        ("cat missing.txt", "cat actual.txt", "error: missing.txt not found"),
        ("cat src/missing", "cat src/actual", "error: not found: src/missing"),
        ("cat src/missing", "cat src/actual", "ERROR: SRC/MISSING NOT FOUND"),
    ],
)
def test_a_missing_path_failure_pairs_with_one_corrected_path(command, fix, output):
    result = pair_session(
        (),
        [
            call("f", START, command=command, status=CallStatus.FAILED, output=output),
            call("fix", START + 1, command=fix),
        ],
        START + 2,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 0)


@pytest.mark.parametrize(
    ("command", "other", "fix"),
    [
        ("cp src/missing.txt dst/a.txt", "cp src/actual.txt dst/b.txt", "cp src/actual.txt dst/a.txt"),
        ("cat src/missing.txt", "cat src/actual.txt extra.txt", "cat src/actual.txt"),
        ("cat src/missing.txt extra.txt", "cat src/actual.txt", "cat src/actual.txt extra.txt"),
        ("git checkout a src/missing.txt", "git checkout b src/missing.txt", "git checkout a src/actual.txt"),
        ("git checkout src/missing.txt", "git show src/actual.txt", "git checkout src/actual.txt"),
        (
            "git -C app checkout src/missing.txt",
            "git -C other checkout src/actual.txt",
            "git -C app checkout src/actual.txt",
        ),
        ("cat src/missing.txt", "cat src/*.txt", "cat src/actual.txt"),
        ("cat src/missing.txt", "cat src/?.txt", "cat src/actual.txt"),
        ("cat src/missing.txt", "cat src/[ab].txt", "cat src/actual.txt"),
        ("cat src/missing.txt", "cat $ROOT/actual.txt", "cat src/actual.txt"),
        ("cat src/missing.txt", "cat ~/actual.txt", "cat src/actual.txt"),
    ],
)
def test_a_path_correction_skips_other_targets_and_waits_for_its_fix(command, other, fix):
    result = pair_session(
        (),
        [
            call(
                "f",
                START,
                command=command,
                status=CallStatus.FAILED,
                output="cat: src/missing.txt: No such file or directory",
            ),
            call("other", START + 1, command=other),
            call("fix", START + 2, command=fix),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


def test_path_corrections_require_a_missing_path_signature_and_path_operands():
    results = []
    for command, fix, output in [
        ("git checkout a", "git checkout b", "fatal: path 'a' does not exist"),
        ("cat src/missing.txt", "cat src/actual.txt", "cat: src/missing.txt: Permission denied"),
        ("cat src/missing.txt", "cat src/actual.txt", "error: unknown option --number"),
        ("cat src/missing.txt", "cat src/actual.txt", "zsh: command not found: cat"),
        ("gh pr diff old.txt", "gh pr diff actual.txt", "gh: Not Found (HTTP 404)"),
        ("cat 'src/*.txt'", "cat src/actual.txt", "cat: src/*.txt: No such file or directory"),
        (
            "cat '$ROOT/missing.txt'",
            "cat src/actual.txt",
            "cat: $ROOT/missing.txt: No such file or directory",
        ),
        ("cat missing.txt", "cat actual.txt", "cat: missing.txt: No such file or directory"),
    ]:
        result = pair_session(
            (),
            [
                call("f", START, command=command, status=CallStatus.FAILED, output=output),
                call("fix", START + 1, command=fix),
            ],
            START + 2,
        )
        results.append((outcome(result), result.operand_mismatch))
    assert results == [
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([], ["f"], 1, 0, 0), 1),
        (([("f", "fix")], [], 1, 0, 0), 0),
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "git -C app push --force origin main",
            Target("git", ("push",), ("origin", "main"), {"-C": ("app",)}),
        ),
        (
            "git worktree add -b B upstream/master /p",
            Target("git", ("worktree", "add"), ("B", "upstream/master", "/p"), {}),
        ),
        ("gh pr diff 1801 --repo o/r", Target("gh", ("pr", "diff"), ("1801",), {"--repo": ("o/r",)})),
        ("kubectl --namespace=a get pods", Target("kubectl", ("get",), ("pods",), {"--namespace": ("a",)})),
        ("tail -n 5 build.log", Target("tail", (), ("build.log",), {})),
        ("rm -f a", Target("rm", (), ("a",), {})),
        (
            "kubectl --kubeconfig cfg get pods",
            Target("kubectl", ("get",), ("pods",), {"--kubeconfig": ("cfg",)}),
        ),
        ("npm run build", Target("npm", ("run",), ("build",), {})),
        ("xcrun simctl boot 1111-AAAA", Target("xcrun", ("simctl", "boot"), ("1111-AAAA",), {})),
        ("xcodebuild -scheme App build", Target("xcodebuild", (), ("build",), {"-scheme": ("App",)})),
        ("make -C app test", Target("make", (), ("test",), {"--directory": ("app",)})),
        ("cat --number -- -notes.md", Target("cat", (), ("-notes.md",), {})),
        ("gh api repos/o/r?ref=x", Target("gh", ("api", "repos/o/r?ref=x"), (), {})),
        ("cat $NOTE", Target("cat", (), ("$NOTE",), {})),
    ],
)
def test_the_target_is_the_subcommand_and_positional_operands(command, expected):
    assert target(command) == expected


def test_commands_of_two_programs_never_share_a_target():
    assert (same_target("cat a", "less a"), same_target("cat a", "cat --number a")) == (False, True)


@pytest.mark.parametrize(
    ("failed_command", "other", "fix"),
    [
        ("sed -n 270-310p f", "sed -n 718,742p g", "sed -n 718,742p f"),
        (r'grep -rln "A" a b', r'grep -rn "A\|B" b a', r'grep -rn "A\|B" a b'),
        ("rg pat a", "rg other b", "rg other a"),
        ("awk '{print $1}' a", "awk '{print $2}' b", "awk '{print $2}' a"),
        ("jq .a f", "jq .a g", "jq .b f"),
        ("sed -n 1,240p a", "sed -n -e 1,180p b", "sed -n -e 1,180p a"),
        ("sed -n 1,240p a", "sed -n --expression=1,180p b", "sed -n --expression=1,180p a"),
        ("sed -n 1,240p a", "sed -n -f script b", "sed -n -f script a"),
        ("sed -n 1,240p a", "sed -n --file script b", "sed -n --file script a"),
        ("grep pat a", "grep -e other b", "grep -e other a"),
        ("grep pat a", "grep --regexp=other b", "grep --regexp=other a"),
        ("grep pat a", "grep -f patterns b", "grep -f patterns a"),
        ("grep pat a", "grep --file patterns b", "grep --file patterns a"),
        ("rg pat a", "rg -e other b", "rg -e other a"),
        ("rg pat a", "rg --regexp other b", "rg --regexp other a"),
        ("rg pat a", "rg -f patterns b", "rg -f patterns a"),
        ("rg pat a", "rg --file=patterns b", "rg --file=patterns a"),
        ("awk '{print $1}' a", "awk -f script b", "awk -f script a"),
        ("jq .a f", "jq -f filter g", "jq -f filter f"),
        ("jq .a f", "jq --from-file filter g", "jq --from-file filter f"),
        ("sed -n 1,240p a", "sed -n -e1,180p b", "sed -n -e1,180p a"),
        ("grep pat a", "grep -eother b", "grep -eother a"),
        ("rg pat a", "rg -eother b", "rg -eother a"),
        ("awk '{print $1}' a", "awk -fscript b", "awk -fscript a"),
        ("jq .a f", "jq -ffilter g", "jq -ffilter f"),
        ("grep -e pat a", "grep -e other b", "grep -e other a"),
        ("grep -f patterns a", "grep -f corrected b", "grep -f corrected a"),
        ("rg -e pat -e more a", "rg -e other -e more b", "rg -e other -e more a"),
        ("sed -ne1p a", "sed -ne2p b", "sed -n -e 2p a"),
        ("rg -- pat a", "rg -- other b", "rg -- other a"),
    ],
)
def test_a_changed_pattern_skips_other_files_and_pairs_on_the_same_files(failed_command, other, fix):
    result = pair_session(
        (),
        [
            failed("f", START, command=failed_command),
            call("other", START + 1, command=other),
            call("fix", START + 2, command=fix),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


def test_a_changed_pattern_without_file_operands_still_pairs():
    result = pair_session(
        (),
        [
            failed("f", START, command='grep -rln "A"'),
            call("fix", START + 1, command=r'grep -rn "A\|B"'),
        ],
        START + 2,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 0)


def test_a_failure_one_second_short_of_a_day_still_pairs():
    late = call("p", START + IDLE_SECONDS - 1)
    result = pair_session(
        (), [failed("f", START, command="pytest --x tests"), late], START + IDLE_SECONDS - 1
    )
    assert outcome(result) == ([("f", "p")], [], 1, 0, 0)


NEW_SELECTORS = [
    ("pnpm", "-C", "test"),
    ("pnpm", "--dir", "test"),
    ("pnpm", "--filter", "test"),
    ("pnpm", "-F", "test"),
    ("yarn", "--cwd", "test"),
    ("uv", "--directory", "sync"),
    ("uv", "--project", "sync"),
    ("xcodebuild", "-project", "build"),
    ("xcodebuild", "-workspace", "build"),
    ("xcodebuild", "-scheme", "build"),
    ("kubectl", "--kubeconfig", "get pods"),
    ("kubectl", "-l", "get pods"),
    ("kubectl", "--selector", "get pods"),
]


@pytest.mark.parametrize(("program", "option", "action"), NEW_SELECTORS)
def test_new_selectors_skip_a_different_target_and_wait_for_the_fix(program, option, action):
    result = pair_session(
        (),
        [
            failed("f", START, command=f"{program} {option}=app {action}"),
            call("other", START + 1, command=f"{program} {option}=other {action}"),
            call("fix", START + 2, command=f"{program} {option} app {action}"),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)


@pytest.mark.parametrize(("program", "option", "action"), NEW_SELECTORS)
def test_adding_or_removing_a_target_selector_still_pairs(program, option, action):
    results = [
        pair_session(
            (),
            [failed("f", START, command=a), call("p", START + 1, command=b)],
            START + 2,
        )
        for a, b in [
            (f"{program} {action}", f"{program} {option} app {action}"),
            (f"{program} {option} app {action}", f"{program} {action}"),
        ]
    ]
    assert [(outcome(result), result.operand_mismatch) for result in results] == [
        (([("f", "p")], [], 1, 0, 0), 0),
        (([("f", "p")], [], 1, 0, 0), 0),
    ]


@pytest.mark.parametrize(
    ("program", "short", "long", "action"),
    [
        ("git", "-C", "-C", "status"),
        ("gh", "-R", "--repo", "pr list"),
        ("npm", "-w", "--workspace", "test"),
        ("make", "-C", "--directory", "test"),
        ("make", "-f", "--file", "test"),
        ("cargo", "-p", "--package", "build"),
        ("kubectl", "-n", "--namespace", "get pods"),
        ("kubectl", "-l", "--selector", "get pods"),
        ("docker", "-H", "--host", "ps"),
        ("docker", "-c", "--context", "ps"),
        ("pnpm", "-C", "--dir", "test"),
        ("pnpm", "-F", "--filter", "test"),
    ],
)
@pytest.mark.parametrize(("value", "other"), [("app", "other"), ("app=one", "other=two")])
def test_attached_short_selectors_compare_values_and_match_separate_aliases(
    program, short, long, action, value, other
):
    result = pair_session(
        (),
        [
            failed("f", START, command=f"{program} {short}{value} {action}"),
            call("other", START + 1, command=f"{program} {short}{other} {action}"),
            call("fix", START + 2, command=f"{program} {long} {value} {action}"),
        ],
        START + 3,
    )
    assert (outcome(result), result.operand_mismatch) == (([("f", "fix")], [], 1, 0, 0), 1)
