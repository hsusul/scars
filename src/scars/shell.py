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

Modelled: words with single quotes, double quotes and backslash escapes; plain `$NAME` variables in
arguments; the separators `&&`, `||`, `;`, `|`, `|&` and `&`; redirections (dropped with their target);
`#` comments at the start of a word; and plain `( ... )` subshell groups that start a command and are
followed only by a separator, a redirection, a closing paren or the end.

An argument the shell expands at run time is reported in `SimpleCommand.uncertain`: one holding an
unquoted or double-quoted `$NAME`, an unquoted glob character (`*`, `?`, `[`), or an unquoted `~`. Its
text is the source text, not the value, and it may expand to no words, several words, or `--`.

Redirections are checked, because bash refuses to run a command whose redirection fails. A redirection
may carry a descriptor number `N` (one digit, written directly before the operator) and takes one target
word with no expansion, glob or `~`. The accepted forms are `N>file`, `N>>file`, `N>|file`, `N<file`,
`N<>file`, `&>file`, `&>>file`, `>&file` (no `N`, and `file` a plain name that does not start with a
digit or `-`), `N>&M` and `N<&M` (M one unquoted digit naming an open descriptor), and `N>&-` and `N<&-`.
Descriptors 0, 1 and 2 start open for each simple command and group; file redirections and dups open
`N`, and `-` closes it. Closing a descriptor on a `( ... )` group is unsupported, since it changes
what the commands inside may use. Anything else after a redirection operator is unsupported, including
`2>&xyz`, `>&5` and a `{name}>` descriptor variable. A command named `exec` or `eval` (or run through
`command` or `builtin`) is unsupported, because it can close descriptors or change the directory of the
commands after it.

Each simple command also reports whether its working directory is known. After a leading `cd DIR &&`,
the directory holds for commands joined by `&&`, `|` or `|&`. After a `;`, `||` or `&`, later commands in
the same group run whether or not the `cd` succeeded, so their directory is unknown. It is also unknown
after a `cd`, `pushd`, `popd`, `source`, `command` or `builtin` that is reported as a command.

The program word must be a bare name of letters, digits and `._+-`, or a path whose every component is such
a name, optionally starting with `/` or `~/`. So a variable, a glob, a trailing `/`, an empty component or
any other character in the program position is unsupported. Words a wrapper consumes before the program
(`env` options, `sudo -u USER`, the `bash -c` script) must not contain a variable or a bare glob, because
word splitting or globbing could change which program runs.

Unsupported: input over 4 KB, any `$` other than `$NAME` (so `${`, `$(`, `$((`, `$[`, `$'`, `$"`, `$1`,
`$?`), backticks, bare brace expansion (`{a,b}`, `{1..3}`), heredocs and herestrings, process
substitution, `((` and `))`, a newline outside single or double quotes, a dangling backslash or quote,
shell keywords at the start of a command (`if`, `for`, `[[`, `{`, `function` and the rest), a `(` that
does not start a command, a word after `)`, unbalanced parens, empty groups, and a separator with no
command before it or a `&&`, `||` or `|` with no command after it.
"""

import dataclasses
import posixpath
import re
from dataclasses import dataclass

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

_REDIRECTS = frozenset({">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<>"})
_NEEDS_FOLLOWER = frozenset({"&&", "||", "|", "|&"})
# Longest first, so a run like `&>>` or `>&` is read as one operator.
_OPERATORS = ("&>>", "&&", "||", "|&", ">>", ">&", "<&", "&>", ">|", "<>", ";", "|", "&", "(", ")", "<", ">")
_OPERATOR_CHARS = frozenset(";&|()<>")
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "case", "esac", "for", "select", "while", "until", "do", "done"}
    | {"in", "function", "coproc", "{", "}", "[[", "]]", "!"}
)
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_DIGITS = re.compile(r"[0-9]+")
_NAMED_FD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_PLAIN_FILE = re.compile(r"[A-Za-z_./][A-Za-z0-9_./+-]*")
_STANDARD_FDS = frozenset({0, 1, 2})
_KEEPS_CWD = frozenset({"&&", "|", "|&"})
_DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd", "source", "command", "builtin"})
_SHELL_STATE = frozenset({"exec", "eval"})
_RUNS_BUILTIN = frozenset({"command", "builtin"})
_NAME_PART = r"[A-Za-z0-9._+-]+"
_PROGRAM = re.compile(rf"(?:/|~/)?(?:{_NAME_PART}/)*{_NAME_PART}")
_SHELLS = frozenset({"bash", "sh"})
_PYTHONS = frozenset({"python", "python3"})


def parse(command: str) -> tuple[SimpleCommand, ...]:
    """Split a shell command into simple commands with wrappers removed.

    Wrappers are removed in one pass in this order, each at most once: `VAR=value` assignments, `env`
    (with `-i`, `-u NAME`), `time` (with `-p`), `nohup`, `sudo` (with `-u USER`), `uv run`, `python -m`
    or `python3 -m` (the module becomes the program and unwrapping stops), `npx`, and `bash -c` or
    `sh -c` (the script is parsed again, one level deep). So `nohup time make` leaves `time` as the
    program. A `cd DIR &&` at the start of the command, or at the start of a subshell group, sets the
    working directory of the commands after it in that group and is not reported as a command. The
    reported program is the basename of the program word.
    """
    return _parse(command, depth=0)


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
            if op in ("<", ">") and command[i + 1 : i + 2] == "(":
                raise Unsupported("process substitution")
            fd = None
            if op[0] in "<>" and word.is_named_fd():
                raise Unsupported("a {name} descriptor variable before a redirection")
            if op[0] in "<>" and word.is_fd_number():
                fd = int("".join(word.chars))
                if fd > 9:
                    raise Unsupported(f"descriptor {fd} above 9")
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
                    self._redirect(text, fd, target)
                    self.pending = True
                    i += 1
                case _Op("("):
                    if self.pending:
                        raise Unsupported("'(' that does not start a command")
                    outer = self.frames[-1]
                    outer.leading = False
                    self.frames.append(_Frame(outer.cwd, outer.cwd_known))
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

    def _redirect(self, op: str, fd: int | None, target: _Word) -> None:
        """Check one redirection against the forms bash runs, tracking which descriptors are open."""
        if not target.literal:
            raise Unsupported(f"redirection target {target.text!r} expands at run time")
        plain = target.quoted_from is None
        fds = self.open_fds
        if op in (">&", "<&"):
            n = fd if fd is not None else (1 if op == ">&" else 0)
            if plain and target.text == "-":
                if self.closed_group:
                    raise Unsupported("closing a descriptor of a group")
                fds.discard(n)
            elif plain and len(target.text) == 1 and target.text.isdigit():
                if int(target.text) not in fds:
                    raise Unsupported(f"descriptor {target.text} may not be open")
                fds.add(n)
            elif op == ">&" and fd is None and plain and _PLAIN_FILE.fullmatch(target.text):
                fds.update((1, 2))
            else:
                raise Unsupported(f"invalid descriptor redirection {op}{target.text!r}")
        elif op in ("&>", "&>>"):
            fds.update((1, 2))
        else:
            fds.add(fd if fd is not None else (0 if op[0] == "<" else 1))

    def _finish(self, separator: str | None) -> None:
        words, self.words = self.words, []
        frame = self.frames[-1]
        self.open_fds = set(_STANDARD_FDS)
        if self.pending:
            frame.has_command = True
            self.needs_follower = False
        if words:
            self._emit(words, frame, separator)
        if frame.moved and separator is not None and separator not in _KEEPS_CWD:
            frame.cwd_known = False

    def _emit(self, words: list[_Word], frame: _Frame, separator: str | None) -> None:
        if words[0].quoted_from is None and words[0].text in _KEYWORDS:
            raise Unsupported(f"shell keyword {words[0].text!r}")
        if frame.leading and separator == "&&" and len(words) == 2 and words[0].text == "cd":
            target = _literal(words[1]).text
            frame.cwd = target if frame.cwd is None else posixpath.join(frame.cwd, target)
            frame.moved = True
            return
        frame.leading = False
        emitted = _unwrap(words, frame.cwd, self.depth)
        for c in emitted:
            if c.program in _SHELL_STATE or (c.program in _RUNS_BUILTIN and _SHELL_STATE & set(c.args)):
                raise Unsupported(f"{c.program} can change the descriptors or directory of later commands")
        if not frame.cwd_known:
            emitted = [dataclasses.replace(c, cwd_known=False) for c in emitted]
        self.commands.extend(emitted)
        if any(c.program in _DIRECTORY_CHANGERS for c in emitted):
            frame.cwd_known = False


def _unwrap(words: list[_Word], cwd: str | None, depth: int) -> list[SimpleCommand]:
    # Only an unquoted `NAME=` is an assignment; `"FOO=1" make` runs a program named `FOO=1`.
    while words and (m := _ASSIGNMENT.match(words[0].text)) and _unquoted_through(words[0], m.end()):
        words = words[1:]
    w = list(words)

    def head(k: int = 0) -> str | None:
        return w[k].text if k < len(w) else None

    def take(k: int) -> None:
        for word in w[:k]:
            _literal(word)
        del w[:k]

    if head() == "env":
        take(1)
        while (arg := head()) is not None:
            if arg in ("-i", "-", "--ignore-environment") or _ASSIGNMENT.match(arg):
                take(1)
            elif arg == "-u" and len(w) > 1:
                take(2)
            elif arg.startswith(("-u", "--unset=")):
                take(1)
            else:
                break
    if head() == "time":
        take(1)
        if head() == "-p":
            take(1)
    if head() == "nohup":
        take(1)
    if head() == "sudo":
        take(1)
        if head() == "-u" and len(w) > 1:
            take(2)
    if head() == "uv" and head(1) == "run":
        take(2)
    if len(w) > 2 and _program(w[0]) in _PYTHONS and w[1].text == "-m":
        take(2)
        return [_simple(w, cwd)]
    if head() == "npx":
        take(1)
    if depth == 0 and len(w) > 2 and _program(w[0]) in _SHELLS and w[1].text == "-c":
        inner = _parse(_literal(w[2]).text, depth=1)
        if cwd is None:
            return list(inner)
        return [dataclasses.replace(c, cwd=posixpath.join(cwd, c.cwd) if c.cwd else cwd) for c in inner]
    if not w:
        return []
    return [_simple(w, cwd)]


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
    args = w[1:]
    uncertain = frozenset(i for i, word in enumerate(args) if not word.literal)
    return SimpleCommand(_program(w[0]), tuple(word.text for word in args), cwd, uncertain)
