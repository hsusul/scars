import os
import random
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("env -- xcodebuild", (cmd("xcodebuild"),)),
        ("env -u FOO -- A=1 xcodebuild", (cmd("xcodebuild"),)),
        ("env - PATH=/bin xcodebuild", (cmd("xcodebuild"),)),
        ("env -uFOO xcodebuild", (cmd("xcodebuild"),)),
        ("sudo -E xcodebuild", (cmd("xcodebuild"),)),
        ("sudo -- xcodebuild", (cmd("xcodebuild"),)),
        ("sudo -n -u ci -E A=1 xcodebuild", (cmd("xcodebuild"),)),
        ("uv run --with x xcodebuild", (cmd("xcodebuild"),)),
        ("uv run --with=x --frozen -- xcodebuild -list", (cmd("xcodebuild", "-list"),)),
        ("npx -y xcodebuild", (cmd("xcodebuild"),)),
        ("nohup -- xcodebuild", (cmd("xcodebuild"),)),
        ("bash -c -- 'xcodebuild'", (cmd("xcodebuild"),)),
        ("bash -lc xcodebuild", (cmd("xcodebuild"),)),
        ("bash -cl xcodebuild", (cmd("xcodebuild"),)),
        ("bash -c -e xcodebuild", (cmd("xcodebuild"),)),
        ("bash -o pipefail -c 'xcodebuild | cat'", (cmd("xcodebuild"), cmd("cat"))),
        ("sh -ec 'xcodebuild' name arg", (cmd("xcodebuild"),)),
        ("bash -x script.sh", (cmd("bash", "-x", "script.sh"),)),
        ("python3 -u -m pytest -q", (cmd("pytest", "-q"),)),
        ("python3 -c 'print(1)'", (cmd("python3", "-c", "print(1)"),)),
        ("nohup time make", (cmd("make"),)),
        ("echo x &> log; xcodebuild >| out 2>&1 <> f", (cmd("echo", "x"), cmd("xcodebuild"))),
    ],
)
def test_parse_unwraps_only_modelled_wrapper_options(command, expected):
    assert parse(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # Options the wrapper table does not model, or that change how the command is found.
        "env -S 'xcodebuild'",
        "env -C /tmp xcodebuild",
        "env -P /bin xcodebuild",
        "env -iu FOO xcodebuild",
        "env --ignore-environment xcodebuild",
        "env --unset=FOO xcodebuild",
        "sudo -i xcodebuild",
        "sudo -s xcodebuild",
        "sudo -En xcodebuild",
        "uv run --directory x xcodebuild",
        "npx -c xcodebuild",
        "npx -- xcodebuild",
        "python3 -I -m xcodebuild",
        "python3 -m a/b",
        "bash -ic xcodebuild",
        "bash --norc -c xcodebuild",
        "bash -c +e xcodebuild",
        "bash -eo pipefail -c xcodebuild",
        "bash -c",
        "time -p xcodebuild",
        "time -- xcodebuild",
        "nohup -p xcodebuild",
        # After an assignment env stops reading options, so it runs `-i`.
        "env A=1 -i xcodebuild",
        "sudo A=1 -u x xcodebuild",
        # A program word never starts with `-`.
        "-xcodebuild",
        "env -- -i xcodebuild",
        # Meaning differs between bash 3.2, bash 5 and zsh.
        "true |& xcodebuild",
        "&>>log xcodebuild",
        "make &>> log",
        "10>f xcodebuild",
        "make 10>&1",
        ">! xcodebuild make",
        "make >>!log",
        "repeat 1 xcodebuild",
        "noglob xcodebuild",
        "nocorrect xcodebuild",
        # zsh reads `time` as a reserved word only first, and reports a parse error after a prefix.
        "A=1 time xcodebuild",
        "true | B=$HOME time xcodebuild",
        "A=1 if true",
    ],
)
def test_parse_rejects_round_three_findings(command):
    with pytest.raises(Unsupported):
        parse(command)


DIFFERENTIAL_CASES = 2400
_REAL_SHELLS = ("/bin/bash", "/bin/zsh")

# sudo, npx and uv run are left out of the differential run: they ask for a password or reach the network.
# `env -i`, `env -` and `bash -l` appear only with an explicit PATH or not at all, because a cleared PATH
# or macOS `path_helper` would run the real /usr/bin/xcodebuild instead of the marker stub.
_TARGETS = (
    "xcodebuild",
    '"xcodebuild"',
    "x'code'build",
    "\\xcodebuild",
    "./tools/xcodebuild",
    "tools/xcodebuild",
    "python3 -m xcodebuild",
    "python3 -u -m xcodebuild",
    "python3 -mxcodebuild",
)
# Every other program must succeed with any argument atoms, so `bash -e` never stops before xcodebuild.
_OTHER_PROGRAMS = ("true", "echo hi", "printf %s x", "python3 -c pass", "echo xcodebuild")
_DECOYS = ("-xcodebuild", "python3 -I -m xcodebuild", "-S xcodebuild", "-- xcodebuild")
_WRAPPER_ATOMS = (
    "env",
    "env -u FOO",
    "env -uFOO",
    "env --",
    "env A=1",
    "env -i PATH={path}",
    "env - PATH={path}",
)
_WRAPPER_ATOMS += ("time", "nohup", "nohup --", "env -u FOO -- A=1")
# Each of these makes the shell run something other than the next word, or is not modelled.
_BAD_WRAPPER_ATOMS = ("env -u FOO A=1 --", "env A=1 -i", "env -S", "env -v", "env -C .", "env --unset=FOO")
_BAD_WRAPPER_ATOMS += ("time -p", "time --", "nohup -p")
_OPTION_ATOMS = ("--", "-x", "-y", "--with x", "-E", "-s", "A=1", "-u FOO")
_REDIRECT_ATOMS = ("> out", ">>out", "2>/dev/null", "2>&1", ">|out", "&>out", "<>rw", "< /dev/null", ">&2")
_REDIRECT_ATOMS += ("1>out", "2> out")
_BAD_REDIRECT_ATOMS = (">! out", "&>>out", "10>out", ">>!out")
_ARG_ATOMS = ("-list", "'a b'", '"$HOME"', "x#y", "xcodebuild", "-x", "';'", "'|'")
_ASSIGNMENT_ATOMS = ("A=1", "A='x y'", "B=$HOME")
_SEPARATORS = (" ; ", "; ", ";", " | ", "|", " & ")
_BAD_SEPARATORS = (" |& ", "|&")
_COMMENTS = (" # xcodebuild", " #; xcodebuild")
_SHELL_WORDS = ("bash", "sh", "/bin/bash")
_SHELL_OPTIONS = ("-c", "-c --", "-ec", "-xc", "-cx", "-e -c", "-c -e", "-o pipefail -c", "+x -c", "-c +x")
_SHELL_OPTIONS += ("-uc", "-eo pipefail -c", "-c -- --", "--norc -c")
_FLAT_ATOMS = _TARGETS + _OTHER_PROGRAMS + _WRAPPER_ATOMS + _BAD_WRAPPER_ATOMS + _OPTION_ATOMS + _ARG_ATOMS
_FLAT_ATOMS += _REDIRECT_ATOMS + _BAD_REDIRECT_ATOMS + _ASSIGNMENT_ATOMS + (";", "|", "#", "bash -c", "sh -c")


def _pick(rng: random.Random, good: tuple[str, ...], bad: tuple[str, ...]) -> str:
    return rng.choice(bad if rng.random() < 0.08 else good)


def _segment(rng: random.Random) -> str:
    parts = []
    if rng.random() < 0.3:
        parts.append(rng.choice(_ASSIGNMENT_ATOMS))
    if rng.random() < 0.15:
        parts.append(_pick(rng, _REDIRECT_ATOMS, _BAD_REDIRECT_ATOMS))
    for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
        parts.append(_pick(rng, _WRAPPER_ATOMS, _BAD_WRAPPER_ATOMS))
        if rng.random() < 0.05:
            parts.append(rng.choice(_OPTION_ATOMS))
    roll = rng.random()
    parts.append(rng.choice(_TARGETS if roll < 0.65 else _OTHER_PROGRAMS if roll < 0.95 else _DECOYS))
    parts.extend(rng.choice(_ARG_ATOMS) for _ in range(rng.randint(0, 2)))
    parts.extend(_pick(rng, _REDIRECT_ATOMS, _BAD_REDIRECT_ATOMS) for _ in range(rng.randint(0, 2)))
    return " ".join(parts)


def _line(rng: random.Random) -> str:
    text = _segment(rng)
    for _ in range(rng.choice((0, 0, 1, 2))):
        text += _pick(rng, _SEPARATORS, _BAD_SEPARATORS) + _segment(rng)
    if rng.random() < 0.15:
        text += rng.choice(_COMMENTS)
    return text


def _differential_command(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.15:
        return " ".join(rng.choice(_FLAT_ATOMS) for _ in range(rng.randint(1, 6)))
    if roll < 0.45:
        prefix = rng.choice(("", "", "env ", "nohup ", "time ", "A=1 "))
        script = shlex.quote(_line(rng))
        suffix = rng.choice(("", "", " name arg"))
        return f"{prefix}{rng.choice(_SHELL_WORDS)} {rng.choice(_SHELL_OPTIONS)} {script}{suffix}"
    return _line(rng)


def _reports_xcodebuild(command: str) -> bool:
    try:
        return any(simple.program == "xcodebuild" for simple in parse(command))
    except Unsupported:
        return False


def _runs_xcodebuild(shell: str, command: str, scratch: Path, env: dict[str, str], bin_dir: Path) -> bool:
    scratch.mkdir()
    # A link, not a fresh copy: macOS assesses each new executable on first run, which costs ~100 ms.
    (scratch / "tools").symlink_to(bin_dir)
    (scratch / "xcodebuild.py").write_text("open('xcodebuild.ran', 'w').close()\n")
    # `wait` lets a backgrounded `xcodebuild &` finish before the shell exits.
    subprocess.run(
        [shell, "-c", command + "\nwait"],
        cwd=scratch,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    marker = scratch / "xcodebuild.ran"
    for _ in range(60):
        if marker.exists():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(
    not all(os.access(shell, os.X_OK) for shell in _REAL_SHELLS),
    reason="needs /bin/bash and /bin/zsh, as on macOS",
)
def test_parse_reports_xcodebuild_only_when_bash_and_zsh_run_it(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("xcodebuild", "python3"):
        body = ": > xcodebuild.ran" if name == "xcodebuild" else f'exec {shlex.quote(sys.executable)} "$@"'
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    path = f"{bin_dir}:/usr/bin:/bin"
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": path, "HOME": str(home), "ZDOTDIR": str(home), "LC_ALL": "C"}

    rng = random.Random(20261029)
    commands = [_differential_command(rng).replace("{path}", path) for _ in range(DIFFERENTIAL_CASES)]
    checked = [c for c in commands if "&&" not in c and "||" not in c and _reports_xcodebuild(c)]
    runs = [
        (shell, c, tmp_path / f"run{i}-{shell[5:]}") for i, c in enumerate(checked) for shell in _REAL_SHELLS
    ]
    with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 2) * 2)) as pool:
        results = list(pool.map(lambda run: _runs_xcodebuild(*run, env, bin_dir), runs))

    missed = [(shell, command) for (shell, command, _), ran in zip(runs, results, strict=True) if not ran]
    assert missed == []
    assert len(checked) > 700, len(checked)


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
        "xcodebuild &>>log",
        "xcodebuild |& cat",
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
        ("make >&log", (cmd("make"),)),
        ("make >|log", (cmd("make"),)),
        ("make 2>>err <in", (cmd("make"),)),
        ("make 0<>f", (cmd("make"),)),
        ("make '>&xyz'", (cmd("make", ">&xyz"),)),
        ("(make) 2>&1 | cat", (cmd("make"), cmd("cat"))),
        ("make # && rm -rf x", (cmd("make"),)),
        ("make;# x", (cmd("make"),)),
    ],
)
def test_parse_accepts_valid_redirections_and_operators(command, expected):
    assert parse(command) == expected
