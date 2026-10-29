import random
import re

import pytest

from scars.shell import SimpleCommand, Unsupported, parse


def cmd(program, *args, cwd=None, uncertain=(), cwd_known=True):
    return SimpleCommand(program, tuple(args), cwd, frozenset(uncertain), cwd_known)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("a && b", (cmd("a"), cmd("b"))),
        ('echo "a && b"', (cmd("echo", "a && b"),)),
        ("bash -c 'xcodebuild -scheme X'", (cmd("xcodebuild", "-scheme", "X"),)),
        ("sh -c 'make test && make lint'", (cmd("make", "test"), cmd("make", "lint"))),
        ("uv run pytest -k x", (cmd("pytest", "-k", "x"),)),
        ("cd apps/web && npm test", (cmd("npm", "test", cwd="apps/web"),)),
        ("env -u FOO TZ=UTC python3 -m pytest", (cmd("pytest"),)),
        ("/usr/bin/xcodebuild -showsdks", (cmd("xcodebuild", "-showsdks"),)),
        ("make > log 2>&1", (cmd("make"),)),
        ("a || b; c | d & e", (cmd("a"), cmd("b"), cmd("c"), cmd("d"), cmd("e"))),
        ("a&&b", (cmd("a"), cmd("b"))),
        ("sort < in.txt >> out.txt", (cmd("sort"),)),
        ("FOO=1 BAR=2 make all", (cmd("make", "all"),)),
        ("env -i PATH=/bin make", (cmd("make"),)),
        ("time nohup make", (cmd("make"),)),
        ("sudo -u bob make install", (cmd("make", "install"),)),
        ("python -m http.server 8000", (cmd("http.server", "8000"),)),
        ("npx tsc --noEmit", (cmd("tsc", "--noEmit"),)),
        ("FOO=1 env -u X time nohup sudo uv run python3 -m pytest -q", (cmd("pytest", "-q"),)),
        ("cd a && cd b && make", (cmd("make", cwd="a/b"),)),
        ("cd a && bash -c 'cd b && make'", (cmd("make", cwd="a/b"),)),
        ("make && cd x && make", (cmd("make"), cmd("cd", "x"), cmd("make", cwd_known=False))),
        ("cd x", (cmd("cd", "x"),)),
        ("(cd x) && make", (cmd("cd", "x"), cmd("make"))),
        ("git log --format=#%h # trailing comment", (cmd("git", "log", "--format=#%h"),)),
        ("echo 'a # b'", (cmd("echo", "a # b"),)),
        ("echo '$(not run)' \"a'b'x\"", (cmd("echo", "$(not run)", "a'b'x"),)),
        ("printf 'line1\nline2'", (cmd("printf", "line1\nline2"),)),
        ("FOO=1", ()),
        ("", ()),
        ("bash -c 'bash -c \"make\"'", (cmd("bash", "-c", "make"),)),
        # Wrappers unwrap once each, in the spec order only.
        ("nohup time make", (cmd("time", "make"),)),
    ],
)
def test_parse_golden_cases(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "$(xcodebuild)",
        "echo `xcodebuild`",
        'echo "$(xcodebuild)"',
        "cat <<EOF\nhello\nEOF",
        "cat <<< word",
        "make\nmake test",
        "make \\\nall",
        'echo "unterminated',
        "make >",
        "make > && b",
        "bash -c 'echo $(id)'",
        "x" * 4097,
    ],
)
def test_parse_rejects_unsupported_forms(command):
    with pytest.raises(Unsupported):
        parse(command)


@pytest.mark.parametrize("command", ["cat <<EOF", "cat <<-EOF", "cat <<< word"])
def test_parse_names_heredocs_as_the_reason(command):
    with pytest.raises(Unsupported, match="heredoc"):
        parse(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo '&&' xcodebuild", (cmd("echo", "&&", "xcodebuild"),)),
        ('echo "&&" xcodebuild', (cmd("echo", "&&", "xcodebuild"),)),
        ("echo \\&\\& xcodebuild", (cmd("echo", "&&", "xcodebuild"),)),
        ("echo ';' xcodebuild", (cmd("echo", ";", "xcodebuild"),)),
        ("echo a'|'b xcodebuild", (cmd("echo", "a|b", "xcodebuild"),)),
        ("echo \\( xcodebuild \\)", (cmd("echo", "(", "xcodebuild", ")"),)),
        ('echo ">" xcodebuild', (cmd("echo", ">", "xcodebuild"),)),
        ('FOO="a b" xcodebuild', (cmd("xcodebuild"),)),
    ],
)
def test_parse_keeps_quoted_and_escaped_operators_as_words(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("123 > out xcodebuild", (cmd("123", "xcodebuild"),)),
        ("echo 2 > f", (cmd("echo", "2"),)),
        ('echo "2">f', (cmd("echo", "2"),)),
        ("make 2>&1", (cmd("make"),)),
        ("make 2>err 1>>out", (cmd("make"),)),
        ("2>err make", (cmd("make"),)),
        ("2&>f xcodebuild", (cmd("2", "xcodebuild"),)),
    ],
)
def test_parse_treats_digits_as_a_file_descriptor_only_when_attached(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "(( xcodebuild ))",
        "((xcodebuild))",
        "echo $((1 + 2)) && xcodebuild",
        "[[ xcodebuild ]]",
        "[[ -f x ]] && xcodebuild",
        "f() ( xcodebuild )",
        "f () { xcodebuild; }",
        "function f { xcodebuild; }",
        "if true; then xcodebuild; fi",
        "for i in 1; do xcodebuild; done",
        "case x in xcodebuild) a;; esac",
        "(a) xcodebuild",
        "x=(xcodebuild)",
        "cat <(xcodebuild)",
        "( )",
        "a )",
        "( a",
        "a && && xcodebuild",
        "; xcodebuild",
        "a &&",
        "a |",
        "echo $'\\x26\\x26' xcodebuild",
        "echo trailing\\",
    ],
)
def test_parse_rejects_compound_syntax_it_does_not_model(command):
    with pytest.raises(Unsupported):
        parse(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("( a; b ) && c", (cmd("a"), cmd("b"), cmd("c"))),
        ("(a;) > log", (cmd("a"),)),
        ("cd a && make && (b)", (cmd("make", cwd="a"), cmd("b", cwd="a"))),
        ("(cd x && make) | cat", (cmd("make", cwd="x"), cmd("cat"))),
        ("cd a && (cd b && make) && make", (cmd("make", cwd="a/b"), cmd("make", cwd="a"))),
        ("make; (cd x && make)", (cmd("make"), cmd("make", cwd="x"))),
    ],
)
def test_parse_tracks_leading_cd_per_group(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m sh -c 'echo hi'", (cmd("sh", "-c", "echo hi"),)),
        ("python3 -m bash -c xcodebuild", (cmd("bash", "-c", "xcodebuild"),)),
        ("python3 -m npx tsc", (cmd("npx", "tsc"),)),
    ],
)
def test_parse_stops_after_python_module(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "echo ${x:-a; xcodebuild; b}",
        "xcodebuild/",
        "*/xcodebuild",
        "echo\x00; xcodebuild",
        "\ud800",
    ],
    ids=["separator-in-parameter-expansion", "trailing-slash", "glob-dir", "nul", "lone-surrogate"],
)
def test_parse_rejects_round_two_findings(command):
    with pytest.raises(Unsupported):
        parse(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd ios && xcodebuild -scheme App", (cmd("xcodebuild", "-scheme", "App", cwd="ios"),)),
        ("env -i xcodebuild", (cmd("xcodebuild"),)),
        ("sudo -u ci xcodebuild", (cmd("xcodebuild"),)),
        ("bash -c 'cd ios && xcodebuild'", (cmd("xcodebuild", cwd="ios"),)),
        ("/usr/bin/xcodebuild -showsdks", (cmd("xcodebuild", "-showsdks"),)),
        ("time nohup xcodebuild", (cmd("xcodebuild"),)),
        ("~/bin/xcodebuild", (cmd("xcodebuild"),)),
        ("./scripts/build.sh -v", (cmd("build.sh", "-v"),)),
        ("echo $HOME; xcodebuild", (cmd("echo", "$HOME", uncertain={0}), cmd("xcodebuild"))),
        ('echo "$HOME/x"', (cmd("echo", "$HOME/x", uncertain={0}),)),
        ("FOO=$BAR xcodebuild", (cmd("xcodebuild"),)),
        ('"xcodebuild" -list', (cmd("xcodebuild", "-list"),)),
        ("bash -c 'echo $X; xcodebuild'", (cmd("echo", "$X", uncertain={0}), cmd("xcodebuild"))),
        (
            "find . -name '*.swift' -exec rm {} \\;",
            (cmd("find", ".", "-name", "*.swift", "-exec", "rm", "{}", ";"),),
        ),
        ("git rebase HEAD@{1}", (cmd("git", "rebase", "HEAD@{1}"),)),
        ("echo '{a,b}' \"{1..3}\"", (cmd("echo", "{a,b}", "{1..3}"),)),
        ('curl -d "{\\"a\\":1,\\"b\\":2}" x', (cmd("curl", "-d", '{"a":1,"b":2}', "x"),)),
        ("ls *.txt", (cmd("ls", "*.txt", uncertain={0}),)),
        ("printf 'tab\there'", (cmd("printf", "tab\there"),)),
    ],
)
def test_parse_keeps_supported_forms_working(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # Expansions and substitutions the parser does not model.
        "echo ${HOME}",
        "echo $[1+1]",
        "echo $1",
        "echo $?",
        "echo $",
        'echo "a$"',
        'echo "${x:-a; xcodebuild}"',
        'echo $"x" xcodebuild',
        "x{a,b}; xcodebuild",
        "echo {1..3}; xcodebuild",
        "echo a{b,c}d",
        # The program is unknown or not a plain name or path.
        "$X",
        "$X xcodebuild",
        '"$CC" -o out main.c',
        '"FOO=1" xcodebuild',
        'F"OO"=1 xcodebuild',
        "xcodebuild*",
        "xcode?uild",
        "[x]codebuild",
        "bin//xcodebuild",
        "~ci/bin/xcodebuild",
        "./",
        "/usr/bin/..",
        "*/bash -c xcodebuild",
        "python3 -m $MOD",
        # A wrapper word that word splitting or globbing could change.
        "env $X xcodebuild",
        "env TZ=$X xcodebuild",
        "sudo -u $U xcodebuild",
        "env -u * xcodebuild",
        'bash -c "$SCRIPT"',
        'bash -c "echo $X; xcodebuild"',
        "bash -c xcode*",
        "cd $DIR && xcodebuild",
        "cd * && xcodebuild",
        # Characters the shell may read differently from Python.
        "echo\x00; xcodebuild",
        "echo a\rxcodebuild",
        "printf 'a\rb'",
        "echo\u2028xcodebuild",
        "echo\u00a0xcodebuild",
        "echo \x1b[0m",
        "\udcff xcodebuild",
    ],
)
def test_parse_rejects_forms_outside_the_allowlist(command):
    with pytest.raises(Unsupported):
        parse(command)


_PIECES = (
    *"abcxyz019_.-/~=*?[]{},:#!",
    *" \t\n'\"\\$`;&|()<>",
    "..",
    "xcodebuild",
    "bash -c ",
    "env ",
    "sudo -u ",
    "cd x && ",
    "python -m ",
    "$HOME",
    "${",
    "$(",
    "$((",
    "$'",
    "2>",
    "&&",
    "((",
    "\x00",
    "\r",
    "\u2028",
    "\u00a0",
    "\ud800",
    "\U0001f600",
)
_PROGRAM_NAME = re.compile(r"[A-Za-z0-9._+-]+")
PROPERTY_CASES = 5000


_WORDS = ("xcodebuild", "make", "-x", "env", "sudo", "-u", "time", "nohup", "cd", "bash", "-c", "uv", "run")
_WORDS += (
    "python",
    "-m",
    "A=1",
    "$HOME",
    "'a b'",
    '"$X"',
    "x/y",
    "~/b",
    "*",
    "{a,b}",
    "&&",
    ";",
    "|",
    "(",
    ")",
)


def _random_command(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return " ".join(rng.choice(_WORDS) for _ in range(rng.randint(0, 10)))
    parts = []
    for _ in range(rng.randint(0, 24)):
        if rng.random() < 0.1:
            raw = bytes(rng.randrange(256) for _ in range(rng.randint(1, 6)))
            parts.append(raw.decode("utf-8", "surrogateescape"))
        else:
            parts.append(rng.choice(_PIECES))
    return "".join(parts)


def test_parse_returns_commands_or_raises_unsupported_for_random_input():
    rng = random.Random(20261029)
    outcomes = {"parsed": 0, "unsupported": 0}
    for _ in range(PROPERTY_CASES):
        command = _random_command(rng)
        try:
            result = parse(command)
        except Unsupported:
            outcomes["unsupported"] += 1
            continue
        outcomes["parsed"] += 1
        assert isinstance(result, tuple)
        for simple in result:
            assert isinstance(simple, SimpleCommand)
            assert _PROGRAM_NAME.fullmatch(simple.program), (command, simple)
    assert sum(outcomes.values()) == PROPERTY_CASES
    assert outcomes["parsed"] > 200 and outcomes["unsupported"] > 200, outcomes


def test_parse_accepts_exactly_4_kb():
    command = "echo " + "x" * 4091
    assert parse(command) == (cmd("echo", "x" * 4091),)


# Arguments whose value the shell decides at run time


@pytest.mark.parametrize(
    ("command", "args", "uncertain"),
    [
        ("xcodebuild $FLAGS", ("$FLAGS",), {0}),
        ('xcodebuild "$FLAGS"', ("$FLAGS",), {0}),
        ("xcodebuild -x a$B", ("-x", "a$B"), {1}),
        ("rm *.o", ("*.o",), {0}),
        ("rm a?c", ("a?c",), {0}),
        ("rm -r[f] x", ("-r[f]", "x"), {0}),
        ("cat ~/x", ("~/x",), {0}),
        ("cp a --to=~/x", ("a", "--to=~/x"), {1}),
        ("echo '$X' '*' \\~ \"*\"", ("$X", "*", "~", "*"), set()),
        ("git push --force", ("push", "--force"), set()),
    ],
)
def test_parse_flags_arguments_the_shell_expands(command, args, uncertain):
    (simple,) = parse(command)
    assert (simple.args, set(simple.uncertain)) == (args, uncertain)


def test_bash_c_keeps_the_uncertain_flags_of_the_inner_script():
    (simple,) = parse("cd ios && bash -c 'xcodebuild $FLAGS'")
    assert (simple.program, simple.args, simple.cwd, simple.uncertain) == (
        "xcodebuild",
        ("$FLAGS",),
        "ios",
        frozenset({0}),
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd ios && make | cat", [("make", "ios", True), ("cat", "ios", True)]),
        ("cd ios && make; xcodebuild", [("make", "ios", True), ("xcodebuild", "ios", False)]),
        ("cd ios && make || xcodebuild", [("make", "ios", True), ("xcodebuild", "ios", False)]),
        ("cd ios && make & xcodebuild", [("make", "ios", True), ("xcodebuild", "ios", False)]),
        ("cd ios && (a; b); c", [("a", "ios", True), ("b", "ios", True), ("c", "ios", False)]),
        ("make && cd x && make", [("make", None, True), ("cd", None, True), ("make", None, False)]),
        ("(cd x; a) && b", [("cd", None, True), ("a", None, False), ("b", None, True)]),
        ("make; xcodebuild", [("make", None, True), ("xcodebuild", None, True)]),
    ],
)
def test_parse_marks_a_directory_that_depends_on_an_earlier_cd_succeeding(command, expected):
    assert [(c.program, c.cwd, c.cwd_known) for c in parse(command)] == expected


# Redirections


@pytest.mark.parametrize(
    "command",
    [
        "xcodebuild 2>&xyz",
        "xcodebuild <&xyz",
        "xcodebuild 2>&-x",
        "xcodebuild 2>&log",
        "xcodebuild >&1x",
        "xcodebuild >&'1'",
        "xcodebuild >&5",
        "xcodebuild 2>&- 1>&2",
        "xcodebuild 3<&4",
        "xcodebuild 10>x",
        "xcodebuild > $F",
        'xcodebuild > "$F"',
        "xcodebuild > *.log",
        "xcodebuild > ~/log",
        "xcodebuild {fd}>x",
        "(xcodebuild) 2>&-",
        "exec 2>&-; xcodebuild >&2",
        "xcodebuild;;",
        "xcodebuild \\",
        "xcodebuild && # x",
        "eval 'exec 2>&-'; xcodebuild >&2",
        "command exec 2>&-; xcodebuild >&2",
        "eval 'cd ios'; xcodebuild",
    ],
)
def test_parse_rejects_redirections_bash_would_refuse_or_could_refuse(command):
    with pytest.raises(Unsupported):
        parse(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("make 2>&1", (cmd("make"),)),
        ("make >&2", (cmd("make"),)),
        ("make 2>& 1", (cmd("make"),)),
        ("make 2>&-", (cmd("make"),)),
        ("make 2<&-", (cmd("make"),)),
        ("make <&0", (cmd("make"),)),
        ("make 3>x 1>&3", (cmd("make"),)),
        ("make 2>&- 2>err 1>&2", (cmd("make"),)),
        ("make &>log", (cmd("make"),)),
        ("make &>>log", (cmd("make"),)),
        ("make >&log", (cmd("make"),)),
        ("make >|log", (cmd("make"),)),
        ("make 2>>err <in", (cmd("make"),)),
        ("make 0<>f", (cmd("make"),)),
        ("make '>&xyz'", (cmd("make", ">&xyz"),)),
        ("(make) 2>&1 | cat", (cmd("make"), cmd("cat"))),
        ("make |& cat", (cmd("make"), cmd("cat"))),
        ("make # && rm -rf x", (cmd("make"),)),
        ("make;# x", (cmd("make"),)),
    ],
)
def test_parse_accepts_valid_redirections_and_operators(command, expected):
    assert parse(command) == expected
