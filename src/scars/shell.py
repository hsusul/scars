"""Bounded shell parser for command triggers.

The parser never guesses. A gate that fails to fire is acceptable; a gate that fires on a program the shell
would not run is not. So the parser accepts only what it models, by allowlist, and raises `Unsupported` for
everything else; the caller then skips command triggers for that call. `parse` raises nothing else.

The tokenizer tracks quoting itself instead of using `shlex`, because `shlex` does not report whether
punctuation was quoted or escaped (`echo '&&' x` would read as two commands) or whether a digit run was
attached to a redirection (`123 > out x` would drop the program `123`).

Accepted characters: printable Unicode, space and tab, plus newline inside quotes. Anything else (NUL and
other control characters, lone surrogates, Unicode line and paragraph separators, non-breaking spaces)
is unsupported, because the shell may read it differently from Python.

Only syntax that means the same in bash 3.2 (macOS `/bin/bash` and `/bin/sh`), bash 5 and zsh is accepted,
because the command may run under any of them.

An argument the shell expands at run time is reported in `SimpleCommand.uncertain`: one holding an
unquoted or double-quoted `$NAME`, an unquoted glob character (`*`, `?`, `[`), or an unquoted `~`. Its
text is the source text, not the value, and it may expand to no words, several words, or `--`.

Redirections are checked, because bash refuses to run a command whose redirection fails. A redirection
may carry a descriptor number `N` (one digit, written directly before the operator) and takes one target
word with no expansion, glob or `~`. The accepted forms are `N>file`, `N>>file`, `N>|file`, `N<file`,
`N<>file`, `&>file`, `>&file` (no `N`, and `file` a plain name that does not start with a digit or `-`),
`N>&M` and `N<&M` (M one unquoted digit naming an open descriptor), and `N>&-` and `N<&-`.
Descriptors 0, 1 and 2 start open for each simple command and group; file redirections and dups open
`N`, and `-` closes it. Closing a descriptor on a `( ... )` group is unsupported, since it changes
what the commands inside may use. Anything else after a redirection operator is unsupported, including
`2>&xyz`, `>&5` and a `{name}>` descriptor variable. A file target that is empty or names a directory by
its spelling (`/`, `.`, `..`, or a trailing `/`) is unsupported too, because bash cannot open it. A command
named `exec` or `eval` (or run through `command` or `builtin`) is unsupported, because it can close
descriptors or change the directory of the commands after it.

A command that can stop or rewire the commands after it is allowed only as the last command: `exit`,
`return`, `logout`, `hash`, `trap`, `ulimit`, `unset`, a `set` with any option other than `-e`, `-u`,
`-x` and `-o pipefail|errexit|nounset|xtrace`, a bare `PATH=` assignment, and `export` (or `declare`,
`typeset`, `readonly`, `local`) of `PATH`. `set -n` and `set -o noexec` run nothing after them, and a new
`PATH` or hash entry can make a name run a different file or none. A `PATH=` prefix on a command is
unsupported for the same reason.

A leading `cd` must have exactly one operand: a literal, non-empty path that does not start with `-` or
`+`. Otherwise (`cd`, `cd -`, `cd --`, `cd -L dir`, `cd ~`, `cd a b`, zsh `cd +1`) the parse is
unsupported, because the directory it moves to is not the operand.

Each simple command also reports whether its working directory is known. After a leading `cd DIR &&`,
the directory holds for commands joined by `&&` or `|`. After a `;`, `||` or `&`, later commands in the
same group run whether or not the `cd` succeeded, so their directory is unknown. It is also unknown
after a `cd`, `pushd`, `popd`, `source`, `command` or `builtin` that is reported as a command.

Modelled: words with single quotes, double quotes and backslash escapes; plain `$NAME` variables in
arguments; the separators `&&`, `||`, `;`, `|` and `&`; the redirections `<`, `>`, `>>`, `<>`, `>|`,
`>&`, `<&` and `&>` (dropped with their target, plus a single digit written directly before a `<` or `>`
operator); `#` comments; and plain `( ... )` subshell groups that start a command and are followed only by
a separator, a redirection, a closing paren or the end.

The program word must be a bare name of letters, digits and `._+-` that does not start with `-`, or a path
whose every component is such a name, optionally starting with `/` or `~/`. So a variable, a glob, a
trailing `/`, an empty component, a leading `-` or any other character in the program position is
unsupported. Words a wrapper consumes before the program (its options, their operands, `env`
assignments, the `bash -c` script) must not contain a variable or a bare glob, because word splitting or
globbing could change which program runs.

Unsupported: input over 4 KB, any `$` other than `$NAME` (so `${`, `$(`, `$((`, `$[`, `$'`, `$"`, `$1`,
`$?`), backticks, bare brace expansion (`{a,b}`, `{1..3}`), heredocs and herestrings, process
substitution, `((` and `))`, a newline outside single or double quotes, a dangling backslash or quote,
shell keywords at the start of a command (`if`, `for`, `[[`, `{`, `!`, `function` and the rest, plus the
zsh-only `repeat`, `foreach`, `noglob` and `nocorrect`), a `(` that does not start a command, a word
after `)`, unbalanced parens, empty groups, and a separator with no command before it or a `&&`, `||` or
`|` with no command after it.

Unsupported because the shells disagree: `|&` and `&>>` (zsh and bash 5 accept them, bash 3.2 reports a
syntax error), a file descriptor of two or more digits such as `10>f` (bash redirects it, zsh runs `10` as
a command), and a redirection target starting with `!` (zsh reads `>!` as a clobbering redirection, bash
as a file named `!`). The same-meaning forms `&>`, `>|` and `<>` stay supported; `<<<` is rejected with
the heredocs and `!` with the keywords.
"""

import posixpath
import re
from dataclasses import dataclass, field, replace
from enum import Enum

MAX_COMMAND_BYTES = 4096


@dataclass(frozen=True)
class SimpleCommand:
    program: str
    args: tuple[str, ...]
    cwd: str | None
    """The directory set by leading `cd` commands, relative to the session cwd unless absolute."""
    uncertain: frozenset[int] = frozenset()
    """Indices into `args` of arguments the shell expands at run time, so their value is unknown."""
    cwd_known: bool = True
    """False when the directory depends on whether an earlier `cd` succeeded."""
    writes: tuple[str, ...] = ()
    """Files its redirections open for writing (`<>` included), including those on an enclosing group, as
    written: a leading `~` is not expanded. With `null_commands`, a command with no program, such as `> f`,
    `X=1 > f`, `(X=1) > f` or the redirection on a leading `cd`, is reported as the null command `:` so its
    targets are kept."""


class Unsupported(Exception):
    """The command uses syntax the parser does not handle, so command triggers are skipped."""


@dataclass(frozen=True)
class _Word:
    text: str
    quoted_from: int | None
    """Index in `text` of the first character that came from quoting or escaping, or None if unquoted."""
    bare: tuple[bool, ...]
    """Per character of `text`: written unquoted and unescaped, so the shell may glob or brace-expand it."""
    expanded: bool
    """Holds a `$NAME` expansion, so the shell's value is unknown."""

    @property
    def literal(self) -> bool:
        """The shell passes exactly `text` as one word: no variable, no bare glob character, no bare `~`."""
        return not self.expanded and not any(
            b and c in "*?[~" for c, b in zip(self.text, self.bare, strict=True)
        )


@dataclass(frozen=True)
class _Op:
    text: str
    fd: int | None = None
    """The descriptor number written directly before a redirection operator."""


_Token = _Word | _Op

_REDIRECTS = frozenset({">", ">>", "<", ">&", "<&", "&>", ">|", "<>"})
_NEEDS_FOLLOWER = frozenset({"&&", "||", "|"})
_SHELL_DEPENDENT = frozenset({"|&", "&>>"})
# Longest first, so a run like `&>>` or `>&` is read as one operator.
_OPERATORS = ("&>>", "&&", "||", "|&", ">>", ">&", "<&", "&>", ">|", "<>", ";", "|", "&", "(", ")", "<", ">")
_OPERATOR_CHARS = frozenset(";&|()<>")
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "case", "esac", "for", "select", "while", "until", "do", "done"}
    | {"in", "function", "coproc", "{", "}", "[[", "]]", "!"}
    | {"repeat", "foreach", "noglob", "nocorrect"}
)
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_DIGITS = re.compile(r"[0-9]+")
_NAMED_FD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_PLAIN_FILE = re.compile(r"[A-Za-z_./][A-Za-z0-9_./+-]*")
_STANDARD_FDS = frozenset({0, 1, 2})
_KEEPS_CWD = frozenset({"&&", "|"})
_DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd", "source", "command", "builtin"})
_SHELL_STATE = frozenset({"exec", "eval"})
_RUNS_BUILTIN = frozenset({"command", "builtin"})
_NAME_PART = r"[A-Za-z0-9._+-]+"
_PROGRAM = re.compile(rf"(?!-)(?:/|~/)?(?:{_NAME_PART}/)*{_NAME_PART}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_MAX_WRAPPERS = 8
_SAFE_SHELL_OPTIONS = frozenset({"pipefail", "errexit", "nounset", "xtrace"})
_SAFE_SET_LETTERS = frozenset("eux")
_ALTERS_LATER = frozenset({"exit", "return", "logout", "hash", "trap", "ulimit", "unset"})
_DECLARES = frozenset({"export", "declare", "typeset", "readonly", "local"})


class _Finds(Enum):
    """How a wrapper names what it runs."""

    COMMAND = "command"
    """The first word after the options (and assignments) is the program, possibly another wrapper."""
    SCRIPT = "script"
    """With the trigger option, the first word after the options is a script that is parsed again."""
    MODULE = "module"
    """With the trigger option, its operand is a module, reported as the program."""


def _words(*words: str) -> frozenset[str]:
    return frozenset(words)


@dataclass(frozen=True)
class _Wrapper:
    """A command that runs another command, and the exact option grammar the parser models for it.

    Unwrapping consumes only these options. Any other word starting with `-` (or `+` where `plus_options`)
    before the wrapped command is unsupported, because an unmodelled option may take an operand or change
    what runs. A SCRIPT or MODULE wrapper whose trigger option is absent is itself the program.
    """

    finds: _Finds
    subcommand: str | None = None
    """A word that must follow the wrapper name, as in `uv run`; without it the wrapper is the program."""
    flags: frozenset[str] = field(default_factory=frozenset)
    operands: frozenset[str] = field(default_factory=frozenset)
    """Options followed by one operand word."""
    attached: bool = True
    """An operand may be attached: `-uNAME` for a short option, `--with=x` for a long one."""
    combined: bool = False
    """Short flags may be combined, as in `-lc`."""
    plus_options: bool = False
    """Words starting with `+` are options too, as in `bash +e`."""
    double_dash: bool = False
    """`--` ends the options."""
    ends_options: frozenset[str] = field(default_factory=frozenset)
    """Options after which no more options are read, as `env -`."""
    stops: frozenset[str] = field(default_factory=frozenset)
    """Options after which every remaining word belongs to the wrapper or its target, as `python -m`."""
    assignments: bool = False
    """`NAME=value` words may follow the options; the first one ends option processing."""
    trigger: str | None = None
    choices: dict[str, frozenset[str]] = field(default_factory=dict)
    """Operand options that accept only these values, as `bash -o pipefail`."""


_SHELL = _Wrapper(
    _Finds.SCRIPT, attached=False, combined=True, plus_options=True, double_dash=True, trigger="-c"
)
_PYTHON = _Wrapper(
    _Finds.MODULE,
    flags=_words("-u", "-B", "-O", "-q"),
    operands=_words("-m", "-c"),
    double_dash=True,
    stops=_words("-m", "-c"),
    trigger="-m",
)
# The `time` keyword takes `-p` in bash but not in zsh, so it gets no options. Options that start a login
# or interactive shell (`-i`, `-s`, `sh -l`) are left out: rc files and `path_helper` can change which
# file a name resolves to, or define an alias or function under that name. A shell takes only the options
# that never stop the script from running; `-n`, `-o noexec` and every other `-o` name, `+` options and
# long options are unsupported. `sh` gets no `-o` at all, since dash may lack `pipefail` and then exits.
_WRAPPERS: dict[str, _Wrapper] = {
    "env": _Wrapper(
        _Finds.COMMAND,
        flags=_words("-i"),
        operands=_words("-u"),
        double_dash=True,
        ends_options=_words("-"),
        assignments=True,
    ),
    "time": _Wrapper(_Finds.COMMAND),
    "nohup": _Wrapper(_Finds.COMMAND, double_dash=True),
    "sudo": _Wrapper(
        _Finds.COMMAND,
        flags=_words("-E", "-H", "-n"),
        operands=_words("-u", "-g"),
        double_dash=True,
        assignments=True,
    ),
    "uv": _Wrapper(
        _Finds.COMMAND,
        subcommand="run",
        flags=_words("--frozen", "--locked", "--no-sync", "--offline", "-q", "--quiet"),
        operands=_words("--with", "--python", "-p"),
        double_dash=True,
    ),
    "npx": _Wrapper(_Finds.COMMAND, flags=_words("-y", "--yes")),
    "python": _PYTHON,
    "python3": _PYTHON,
    "bash": replace(
        _SHELL,
        flags=_words("-c", "-e", "-u", "-x", "-l"),
        operands=_words("-o"),
        choices={"-o": _SAFE_SHELL_OPTIONS},
    ),
    "sh": replace(_SHELL, flags=_words("-c", "-e", "-u", "-x")),
}


def parse(command: str, *, null_commands: bool = False) -> tuple[SimpleCommand, ...]:
    """Split a shell command into simple commands with wrappers removed.

    Leading `VAR=value` assignments are dropped, then wrappers from the wrapper table are removed in any
    order, up to eight deep: `env`, `time`, `nohup`, `sudo`, `uv run` and `npx` run the first word after
    their options; `python -m` and `python3 -m` report the module as the program; `bash -c` and `sh -c`
    parse their script again, one level deep. A `cd DIR &&` at the start of the command, or at the start
    of a subshell group, sets the working directory of the commands after it in that group and is not
    reported as a command. The reported program is the basename of the program word.

    `null_commands` also reports redirections that run no program as the null command `:` (see
    `SimpleCommand.writes`). `:` is never a program word the parser accepts, so it cannot be confused with
    one; callers that ask what runs leave it off.
    """
    commands = _parse(command, depth=0)
    return commands if null_commands else tuple(c for c in commands if c.program != ":")


def _parse(command: str, depth: int) -> tuple[SimpleCommand, ...]:
    if len(command) > MAX_COMMAND_BYTES:
        raise Unsupported("command longer than 4 KB")
    for ch in command:
        if not (ch.isprintable() or ch in " \t\n"):
            raise Unsupported(f"unsupported character {ch!r}")
    if len(command.encode()) > MAX_COMMAND_BYTES:
        raise Unsupported("command longer than 4 KB")
    return tuple(_Grouper(depth).run(_tokenize(command)))


class _WordBuilder:
    def __init__(self) -> None:
        self.chars: list[str] = []
        self.bare: list[bool] = []
        self.quoted_from: int | None = None
        self.started = False
        """A word is open, even if still empty, as after `''`."""
        self.expanded = False

    def add(self, text: str, *, bare: bool) -> None:
        if not bare and self.quoted_from is None:
            self.quoted_from = len(self.chars)
        self.chars.extend(text)
        self.bare.extend([bare] * len(text))
        self.started = True

    def add_variable(self, text: str) -> None:
        self.chars.extend(text)
        self.bare.extend([False] * len(text))
        self.started = self.expanded = True

    def is_fd_number(self) -> bool:
        return self.quoted_from is None and not self.expanded and bool(_DIGITS.fullmatch("".join(self.chars)))

    def is_named_fd(self) -> bool:
        return (
            self.quoted_from is None and not self.expanded and bool(_NAMED_FD.fullmatch("".join(self.chars)))
        )

    def build(self) -> _Word:
        bare = tuple(self.bare)
        if _has_brace_expansion(self.chars, bare):
            raise Unsupported("brace expansion")
        return _Word("".join(self.chars), self.quoted_from, bare, self.expanded)


def _has_brace_expansion(chars: list[str], bare: tuple[bool, ...]) -> bool:
    """A bare `{` followed by a bare `,` or `..` and then a bare `}`. Over-approximates, never misses."""
    opened = separated = False
    for j, (c, b) in enumerate(zip(chars, bare, strict=True)):
        if not b:
            continue
        if c == "{":
            opened = True
        elif opened and (
            c == "," or (c == "." and j + 1 < len(chars) and chars[j + 1] == "." and bare[j + 1])
        ):
            separated = True
        elif c == "}" and separated:
            return True
    return False


def _tokenize(command: str) -> list[_Token]:
    tokens: list[_Token] = []
    word = _WordBuilder()

    def end_word() -> None:
        nonlocal word
        if word.started:
            tokens.append(word.build())
        word = _WordBuilder()

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        pair = command[i : i + 2]
        if ch in " \t":
            end_word()
            i += 1
        elif ch == "\n":
            raise Unsupported("newline outside quotes")
        elif ch == "\\":
            if i + 1 >= n or command[i + 1] == "\n":
                raise Unsupported("backslash at the end of a line")
            word.add(command[i + 1], bare=False)
            i += 2
        elif ch == "'":
            close = command.find("'", i + 1)
            if close < 0:
                raise Unsupported("unterminated single quote")
            word.add(command[i + 1 : close], bare=False)
            i = close + 1
        elif ch == '"':
            word.add("", bare=False)
            i = _read_double_quoted(command, i + 1, word)
        elif ch == "$":
            i = _read_variable(command, i, word)
        elif ch == "`":
            raise Unsupported("command substitution")
        elif ch == "#" and not word.started:
            break
        elif ch in _OPERATOR_CHARS:
            if pair in ("((", "))"):
                raise Unsupported("arithmetic or nested group syntax")
            if pair == "<<":
                raise Unsupported("heredoc or herestring")
            op = next(o for o in _OPERATORS if command.startswith(o, i))
            if op in _SHELL_DEPENDENT:
                raise Unsupported(f"{op!r} means different things in bash 3.2, bash 5 and zsh")
            if op in ("<", ">") and command[i + 1 : i + 2] == "(":
                raise Unsupported("process substitution")
            fd = None
            if op[0] in "<>" and word.is_named_fd():
                raise Unsupported("a {name} descriptor variable before a redirection")
            if op[0] in "<>" and word.is_fd_number():
                if len(word.chars) > 1:
                    raise Unsupported("zsh reads a multi-digit file descriptor as a word")
                fd = int(word.chars[0])
                word = _WordBuilder()
            end_word()
            tokens.append(_Op(op, fd))
            i += len(op)
        else:
            word.add(ch, bare=True)
            i += 1
    end_word()
    return tokens


def _read_variable(command: str, i: int, word: _WordBuilder) -> int:
    """Append the plain `$NAME` at `i` to `word`; return the index after it. Other `$` forms raise."""
    m = _VARIABLE.match(command, i)
    if m is None:
        raise Unsupported(f"unsupported expansion {command[i : i + 2]!r}")
    word.add_variable(m.group())
    return m.end()


def _read_double_quoted(command: str, i: int, word: _WordBuilder) -> int:
    """Append the body of a double-quoted string starting at `i`; return the index after the closing quote."""
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == '"':
            return i + 1
        if ch == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "\n":
                raise Unsupported("line continuation inside double quotes")
            word.add(nxt if nxt in '$`"\\' else ch + nxt, bare=False)
            i += 2
        elif ch == "$":
            i = _read_variable(command, i, word)
        elif ch == "`":
            raise Unsupported("command substitution")
        else:
            word.add(ch, bare=False)
            i += 1
    raise Unsupported("unterminated double quote")


@dataclass
class _Frame:
    """One nesting level: the top level or a `( ... )` group."""

    cwd: str | None
    cwd_known: bool = True
    leading: bool = True
    """No command has run yet in this group, so a `cd DIR &&` here still sets the group's directory."""
    has_command: bool = False
    moved: bool = False
    """A leading `cd` in this group set its directory."""


class _Grouper:
    """Walk the tokens once, checking the small grammar the parser models and collecting simple commands."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.frames = [_Frame(None)]
        self.commands: list[SimpleCommand] = []
        self.words: list[_Word] = []
        self.pending = False
        """The current command has content: words, a redirection or a closed group."""
        self.closed_group = False
        self.needs_follower = False
        self.open_fds = set(_STANDARD_FDS)
        self.altered = False
        """A command that can stop or rewire later commands has been emitted."""
        self.writes: list[str] = []
        self.group_starts: list[int] = []
        self.closed_from = 0
        """Index of the first command inside the group that just closed."""

    def run(self, tokens: list[_Token]) -> list[SimpleCommand]:
        i = 0
        while i < len(tokens):
            match tokens[i]:
                case _Word() as word:
                    if self.closed_group:
                        raise Unsupported("a word after a closing paren")
                    self.words.append(word)
                    self.pending = True
                case _Op(text, fd) if text in _REDIRECTS:
                    target = tokens[i + 1] if i + 1 < len(tokens) else None
                    if not isinstance(target, _Word):
                        raise Unsupported("redirection without a target")
                    if target.text.startswith("!") and target.bare[0]:
                        raise Unsupported("zsh reads `>!` as a clobbering redirection")
                    if self._redirect(text, fd, target):
                        if self.closed_group:
                            start = self.closed_from
                            if start == len(self.commands):
                                self._emit_null(self.frames[-1], ())
                            self.commands[start:] = [
                                replace(c, writes=(*c.writes, target.text)) for c in self.commands[start:]
                            ]
                        else:
                            self.writes.append(target.text)
                    self.pending = True
                    i += 1
                case _Op("("):
                    if self.pending:
                        raise Unsupported("'(' that does not start a command")
                    outer = self.frames[-1]
                    outer.leading = False
                    self.frames.append(_Frame(outer.cwd, outer.cwd_known))
                    self.group_starts.append(len(self.commands))
                    self.needs_follower = False
                    self.open_fds = set(_STANDARD_FDS)
                case _Op(")"):
                    if len(self.frames) == 1:
                        raise Unsupported("unbalanced ')'")
                    if self.needs_follower and not self.pending:
                        raise Unsupported("operator with no command after it")
                    self._finish(None)
                    if not self.frames.pop().has_command:
                        raise Unsupported("empty group")
                    self.closed_from = self.group_starts.pop()
                    self.frames[-1].has_command = True
                    self.pending = self.closed_group = True
                    self.open_fds = set(_STANDARD_FDS)
                case _Op(text):
                    if not self.pending:
                        raise Unsupported(f"{text!r} with no command before it")
                    self._finish(text)
                    self.pending = self.closed_group = False
                    self.needs_follower = text in _NEEDS_FOLLOWER
            i += 1
        if len(self.frames) > 1:
            raise Unsupported("unbalanced '('")
        if self.needs_follower and not self.pending:
            raise Unsupported("operator with no command after it")
        self._finish(None)
        return self.commands

    def _redirect(self, op: str, fd: int | None, target: _Word) -> bool:
        """Check one redirection against the forms bash runs, tracking which descriptors are open.

        True when it opens its target as a file for writing.
        """
        if not target.literal and not _home_relative(target):
            raise Unsupported(f"redirection target {target.text!r} expands at run time")
        if target.text == "":
            raise Unsupported("bash cannot open an empty redirection target")

        plain = target.quoted_from is None
        fds = self.open_fds
        if op in (">&", "<&"):
            n = fd if fd is not None else (1 if op == ">&" else 0)
            if plain and target.text == "-":
                if self.closed_group:
                    raise Unsupported("closing a descriptor of a group")
                fds.discard(n)
                return False
            if plain and len(target.text) == 1 and target.text.isdigit():
                if int(target.text) not in fds:
                    raise Unsupported(f"descriptor {target.text} may not be open")
                fds.add(n)
                return False
            if op == ">&" and fd is None and plain and _PLAIN_FILE.fullmatch(target.text):
                _check_file_target(target.text)
                fds.update((1, 2))
                return True
            raise Unsupported(f"invalid descriptor redirection {op}{target.text!r}")
        _check_file_target(target.text)
        if op == "&>":
            fds.update((1, 2))
        else:
            fds.add(fd if fd is not None else (0 if op[0] == "<" else 1))
        return op != "<"

    def _finish(self, separator: str | None) -> None:
        words, self.words = self.words, []
        writes, self.writes = tuple(self.writes), []
        frame = self.frames[-1]
        self.open_fds = set(_STANDARD_FDS)
        if self.pending:
            frame.has_command = True
            self.needs_follower = False
        if words:
            self._emit(words, frame, separator, writes)
        elif writes:
            self._emit_null(frame, writes)
        if frame.moved and separator is not None and separator not in _KEEPS_CWD:
            frame.cwd_known = False

    def _emit(
        self, words: list[_Word], frame: _Frame, separator: str | None, writes: tuple[str, ...]
    ) -> None:
        if words[0].quoted_from is None and words[0].text in _KEYWORDS:
            raise Unsupported(f"shell keyword {words[0].text!r}")
        if frame.leading and words[0].text == "cd":
            if len(words) != 2 or not _plain_directory(words[1]):
                raise Unsupported("a leading cd needs exactly one plain directory operand")
            if separator == "&&":
                if writes:
                    self._emit_null(frame, writes)
                target = words[1].text
                frame.cwd = target if frame.cwd is None else posixpath.join(frame.cwd, target)
                frame.moved = True
                return
        frame.leading = False
        if self.altered:
            raise Unsupported("an earlier command can stop or rewire this one")
        if _assigns_path(words):
            self.altered = True
        emitted = _unwrap(words, frame.cwd, self.depth)
        for c in emitted:
            if c.program in _SHELL_STATE or (c.program in _RUNS_BUILTIN and _SHELL_STATE & set(c.args)):
                raise Unsupported(f"{c.program} can change the descriptors or directory of later commands")
        if any(_alters_later(c) for c in emitted):
            self.altered = True
        if not frame.cwd_known:
            emitted = [replace(c, cwd_known=False) for c in emitted]
        if writes and not emitted:
            self._emit_null(frame, writes)
        emitted = [replace(c, writes=(*c.writes, *writes)) for c in emitted]
        self.commands.extend(emitted)
        if any(c.program in _DIRECTORY_CHANGERS for c in emitted):
            frame.cwd_known = False

    def _emit_null(self, frame: _Frame, writes: tuple[str, ...]) -> None:
        self.commands.append(SimpleCommand(":", (), frame.cwd, cwd_known=frame.cwd_known, writes=writes))


def _home_relative(word: _Word) -> bool:
    """`~` or `~/rest` with nothing else the shell expands: bash and zsh both open the home path."""
    tail = _Word(word.text[1:], None, word.bare[1:], word.expanded)
    return word.text[:1] == "~" and word.bare[0] and word.text[1:2] in ("", "/") and tail.literal


def _check_file_target(text: str) -> None:
    if text.endswith("/") or text == "~" or text.rsplit("/", 1)[-1] in (".", ".."):
        raise Unsupported(f"redirection target {text!r} is a directory")


def _plain_directory(word: _Word) -> bool:
    return word.literal and word.text != "" and word.text[0] not in "-+"


def _is_assignment(word: _Word) -> bool:
    """Only an unquoted `NAME=` is an assignment; `"FOO=1" make` runs a program named `FOO=1`."""
    m = _ASSIGNMENT.match(word.text)
    return m is not None and _unquoted_through(word, m.end())


def _assigns_path(words: list[_Word]) -> bool:
    """The words are only shell assignments, and one of them sets `PATH`."""
    return all(_is_assignment(w) for w in words) and any(w.text.startswith("PATH=") for w in words)


def _alters_later(c: SimpleCommand) -> bool:
    """The command can stop the commands after it, or change which file a name runs."""
    if c.program in _RUNS_BUILTIN:
        return bool((_ALTERS_LATER | _DECLARES | {"set"}) & set(c.args))
    if c.program in _DECLARES:
        return any(a.startswith("PATH") for a in c.args)
    if c.program == "set":
        return not _safe_set(c.args)
    return c.program in _ALTERS_LATER


def _safe_set(args: tuple[str, ...]) -> bool:
    i = 0
    while i < len(args):
        if args[i] == "-o" and i + 1 < len(args) and args[i + 1] in _SAFE_SHELL_OPTIONS:
            i += 2
        elif len(args[i]) > 1 and args[i][0] == "-" and set(args[i][1:]) <= _SAFE_SET_LETTERS:
            i += 1
        else:
            return False
    return True


def _unwrap(words: list[_Word], cwd: str | None, depth: int) -> list[SimpleCommand]:
    prefixed = False
    while words and _is_assignment(words[0]):
        if words[0].text.startswith("PATH=") and len(words) > 1:
            raise Unsupported("a PATH= prefix changes which file the command runs")
        words, prefixed = words[1:], True
    # After an assignment bash runs `/usr/bin/time` while zsh still reads the reserved word and fails.
    if prefixed and words and words[0].quoted_from is None and words[0].text in _KEYWORDS | {"time"}:
        raise Unsupported(f"reserved word {words[0].text!r} after an assignment")
    for _ in range(_MAX_WRAPPERS):
        if not words:
            return []
        name = _program(words[0])
        wrapper = _WRAPPERS.get(name)
        # A nested shell is reported as itself: the parser follows one level of `-c` scripts.
        if wrapper is None or (wrapper.finds is _Finds.SCRIPT and depth > 0):
            return [_simple(words, cwd)]
        args = words[1:]
        if wrapper.subcommand is not None:
            if not args or args[0].text != wrapper.subcommand:
                return [_simple(words, cwd)]
            args = args[1:]
        seen, rest = _scan_options(name, wrapper, args)
        if wrapper.finds is _Finds.COMMAND:
            words = rest
            continue
        if wrapper.trigger not in seen:
            return [_simple(words, cwd)]
        if wrapper.finds is _Finds.MODULE:
            module = seen[wrapper.trigger] or ""
            if not _MODULE.fullmatch(module):
                raise Unsupported(f"module {module!r} is not a dotted name")
            return [_command(module, rest, cwd)]
        if not rest:
            raise Unsupported(f"{name} {wrapper.trigger} has no script")
        inner = _parse(_literal(rest[0]).text, depth=depth + 1)
        if cwd is None:
            return list(inner)
        return [replace(c, cwd=posixpath.join(cwd, c.cwd) if c.cwd else cwd) for c in inner]
    raise Unsupported(f"more than {_MAX_WRAPPERS} nested wrappers")


def _scan_options(
    name: str, wrapper: _Wrapper, args: list[_Word]
) -> tuple[dict[str, str | None], list[_Word]]:
    """Consume the wrapper's modelled options and assignments from `args`.

    Returns each option seen with its operand (None for a flag), and the words after the options.
    """
    seen: dict[str, str | None] = {}
    i = 0
    while i < len(args):
        text = _literal(args[i]).text
        if wrapper.double_dash and text == "--":
            i += 1
            break
        if text in wrapper.ends_options:
            seen[text] = None
            i += 1
            break
        if not (text.startswith("-") or (wrapper.plus_options and text.startswith("+"))):
            break
        i += 1
        if text in wrapper.flags:
            seen[text] = None
        elif text in wrapper.operands:
            if i >= len(args):
                raise Unsupported(f"{name} option {text!r} has no operand")
            seen[text] = _operand(name, wrapper, text, _literal(args[i]).text)
            i += 1
        elif wrapper.attached and (attached := _attached_operand(text, wrapper.operands)):
            seen[attached[0]] = _operand(name, wrapper, attached[0], attached[1])
        elif wrapper.combined and _is_flag_cluster(text, wrapper.flags):
            seen.update(dict.fromkeys(f"-{letter}" for letter in text[1:]))
        else:
            raise Unsupported(f"{name} option {text!r} is not modelled")
        if wrapper.stops & seen.keys():
            break
    rest = args[i:]
    if wrapper.assignments:
        while rest and _ASSIGNMENT.match(_literal(rest[0]).text):
            rest = rest[1:]
    return seen, rest


def _operand(name: str, wrapper: _Wrapper, option: str, value: str) -> str:
    """An empty operand, or one that looks like an option, makes the wrapper fail or parse it differently."""
    if value == "" or value.startswith("-"):
        raise Unsupported(f"{name} option {option!r} has operand {value!r}")
    if option in wrapper.choices and value not in wrapper.choices[option]:
        raise Unsupported(f"{name} option {option} {value!r} is not modelled")
    return value


def _attached_operand(text: str, operands: frozenset[str]) -> tuple[str, str] | None:
    if text.startswith("--"):
        option, equals, value = text.partition("=")
        return (option, value) if equals and option in operands else None
    if len(text) > 2 and text[:2] in operands:
        return text[:2], text[2:]
    return None


def _is_flag_cluster(text: str, flags: frozenset[str]) -> bool:
    return len(text) > 2 and text[0] == "-" and text[1] != "-" and all(f"-{c}" in flags for c in text[1:])


def _literal(word: _Word) -> _Word:
    if not word.literal:
        raise Unsupported(f"{word.text!r} holds a variable, glob or '~' that decides what runs")
    return word


def _program(word: _Word) -> str:
    """The basename of a program word that passes the allowlist."""
    if word.expanded or not _PROGRAM.fullmatch(word.text):
        raise Unsupported(f"program word {word.text!r} is not a plain name or path")
    name = word.text.rsplit("/", 1)[-1]
    if name in (".", ".."):
        raise Unsupported(f"program word {word.text!r} names a directory")
    return name


def _unquoted_through(word: _Word, end: int) -> bool:
    return word.quoted_from is None or word.quoted_from >= end


def _simple(w: list[_Word], cwd: str | None) -> SimpleCommand:
    return _command(_program(w[0]), w[1:], cwd)


def _command(program: str, args: list[_Word], cwd: str | None) -> SimpleCommand:
    uncertain = frozenset(i for i, word in enumerate(args) if not word.literal)
    return SimpleCommand(program, tuple(word.text for word in args), cwd, uncertain)
