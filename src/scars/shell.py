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
        """The shell passes exactly `text` as one word: no variable and no bare glob character."""
        return not self.expanded and not any(
            b and c in "*?[" for c, b in zip(self.text, self.bare, strict=True)
        )


@dataclass(frozen=True)
class _Op:
    text: str


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
_NAME_PART = r"[A-Za-z0-9._+-]+"
_PROGRAM = re.compile(rf"(?!-)(?:/|~/)?(?:{_NAME_PART}/)*{_NAME_PART}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_MAX_WRAPPERS = 8


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
# file a name resolves to, or define an alias or function under that name.
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
    "bash": replace(_SHELL, flags=_words("-c", "-e", "-u", "-x", "-l"), operands=_words("-o")),
    "sh": replace(_SHELL, flags=_words("-c", "-e", "-u", "-x")),
}


def parse(command: str) -> tuple[SimpleCommand, ...]:
    """Split a shell command into simple commands with wrappers removed.

    Leading `VAR=value` assignments are dropped, then wrappers from the wrapper table are removed in any
    order, up to eight deep: `env`, `time`, `nohup`, `sudo`, `uv run` and `npx` run the first word after
    their options; `python -m` and `python3 -m` report the module as the program; `bash -c` and `sh -c`
    parse their script again, one level deep. A `cd DIR &&` at the start of the command, or at the start
    of a subshell group, sets the working directory of the commands after it in that group and is not
    reported as a command. The reported program is the basename of the program word.
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
            if op[0] in "<>" and word.is_fd_number():
                if len(word.chars) > 1:
                    raise Unsupported("zsh reads a multi-digit file descriptor as a word")
                word = _WordBuilder()
            end_word()
            tokens.append(_Op(op))
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
    leading: bool = True
    """No command has run yet in this group, so a `cd DIR &&` here still sets the group's directory."""
    has_command: bool = False


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

    def run(self, tokens: list[_Token]) -> list[SimpleCommand]:
        i = 0
        while i < len(tokens):
            match tokens[i]:
                case _Word() as word:
                    if self.closed_group:
                        raise Unsupported("a word after a closing paren")
                    self.words.append(word)
                    self.pending = True
                case _Op(text) if text in _REDIRECTS:
                    target = tokens[i + 1] if i + 1 < len(tokens) else None
                    if not isinstance(target, _Word):
                        raise Unsupported("redirection without a target")
                    if target.text.startswith("!") and target.bare[0]:
                        raise Unsupported("zsh reads `>!` as a clobbering redirection")
                    self.pending = True
                    i += 1
                case _Op("("):
                    if self.pending:
                        raise Unsupported("'(' that does not start a command")
                    outer = self.frames[-1]
                    outer.leading = False
                    self.frames.append(_Frame(outer.cwd))
                    self.needs_follower = False
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

    def _finish(self, separator: str | None) -> None:
        words, self.words = self.words, []
        frame = self.frames[-1]
        if self.pending:
            frame.has_command = True
            self.needs_follower = False
        if not words:
            return
        if words[0].quoted_from is None and words[0].text in _KEYWORDS:
            raise Unsupported(f"shell keyword {words[0].text!r}")
        if frame.leading and separator == "&&" and len(words) == 2 and words[0].text == "cd":
            target = _literal(words[1]).text
            frame.cwd = target if frame.cwd is None else posixpath.join(frame.cwd, target)
            return
        frame.leading = False
        self.commands.extend(_unwrap(words, frame.cwd, self.depth))


def _unwrap(words: list[_Word], cwd: str | None, depth: int) -> list[SimpleCommand]:
    # Only an unquoted `NAME=` is an assignment; `"FOO=1" make` runs a program named `FOO=1`.
    prefixed = False
    while words and (m := _ASSIGNMENT.match(words[0].text)) and _unquoted_through(words[0], m.end()):
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
            return [SimpleCommand(module, tuple(word.text for word in rest), cwd)]
        if not rest:
            raise Unsupported(f"{name} {wrapper.trigger} has no script")
        inner = _parse(_literal(rest[0]).text, depth=depth + 1)
        if cwd is None:
            return list(inner)
        return [SimpleCommand(c.program, c.args, posixpath.join(cwd, c.cwd) if c.cwd else cwd) for c in inner]
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
            seen[text] = _literal(args[i]).text
            i += 1
        elif wrapper.attached and (attached := _attached_operand(text, wrapper.operands)):
            seen[attached[0]] = attached[1]
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
        raise Unsupported(f"{word.text!r} holds a variable or glob that decides which program runs")
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
    return SimpleCommand(_program(w[0]), tuple(word.text for word in w[1:]), cwd)
