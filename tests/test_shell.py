import pytest

from scars.shell import SimpleCommand, Unsupported, parse


def cmd(program, *args, cwd=None):
    return SimpleCommand(program, tuple(args), cwd)


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
        ("make && cd x && make", (cmd("make"), cmd("cd", "x"), cmd("make"))),
        ("cd x", (cmd("cd", "x"),)),
        ("(cd x && make) | cat", (cmd("cd", "x"), cmd("make"), cmd("cat"))),
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


def test_parse_accepts_exactly_4_kb():
    command = "echo " + "x" * 4091
    assert parse(command) == (cmd("echo", "x" * 4091),)
