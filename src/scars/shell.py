"""Bounded shell parser for command triggers.

The parser never guesses. A gate that fails to fire is acceptable; a gate that fires on a program the shell
would not run is not. So any syntax the parser does not model raises `Unsupported`, and the caller skips
command triggers for that call.

The tokenizer tracks quoting itself instead of using `shlex`, because `shlex` does not report whether
punctuation was quoted or escaped (`echo '&&' x` would read as two commands) or whether a digit run was
attached to a redirection (`123 > out x` would drop the program `123`).

Modelled: words with single quotes, double quotes and backslash escapes; the separators `&&`, `||`, `;`,
`|`, `|&` and `&`; redirections (dropped with their target, plus a digit run written directly before a
`<` or `>` operator); `#` comments; and plain `( ... )` subshell groups that start a command and are
followed only by a separator, a redirection, a closing paren or the end.

Unsupported: input over 4 KB, `$(`, `$((`, backticks, `$'...'` and `$"..."` quoting, heredocs and
herestrings, process substitution, `((` and `))`, a newline outside single or double quotes, a dangling
backslash or quote, shell keywords at the start of a command (`if`, `for`, `[[`, `{`, `function` and the
rest), a `(` that does not start a command, a word after `)`, unbalanced parens, empty groups, and a
separator with no command before it or a `&&`, `||` or `|` with no command after it.
"""

import posixpath
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _Op:
    text: str


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
_DIGITS = re.compile(r"[0-9]+")
_SHELLS = frozenset({"bash", "sh"})
_PYTHONS = frozenset({"python", "python3"})


def parse(command: str) -> tuple[SimpleCommand, ...]:
    """Split a shell command into simple commands with wrappers removed.

    Wrappers are removed in one pass in this order, each at most once: `VAR=value` assignments, `env`
    (with `-i`, `-u NAME`), `time` (with `-p`), `nohup`, `sudo` (with `-u USER`), `uv run`, `python -m`
    or `python3 -m` (the module becomes the program and unwrapping stops), `npx`, and `bash -c` or
    `sh -c` (the script is parsed again, one level deep). So `nohup time make` leaves `time` as the
    program. A `cd DIR &&` at the start of the command, or at the start of a subshell group, sets the
    working directory of the commands after it in that group and is not reported as a command.
    """
    return _parse(command, depth=0)


def _parse(command: str, depth: int) -> tuple[SimpleCommand, ...]:
    if len(command.encode()) > MAX_COMMAND_BYTES:
        raise Unsupported("command longer than 4 KB")
    return tuple(_Grouper(depth).run(_tokenize(command)))


def _tokenize(command: str) -> list[_Token]:
    tokens: list[_Token] = []
    buf: list[str] = []
    quoted_from: int | None = None
    started = False  # a word is open, even if still empty, as after `''`

    def end_word() -> None:
        nonlocal buf, quoted_from, started
        if started:
            tokens.append(_Word("".join(buf), quoted_from))
        buf, quoted_from, started = [], None, False

    def mark_quoted() -> None:
        nonlocal quoted_from, started
        if quoted_from is None:
            quoted_from = len("".join(buf))
        started = True

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        pair = command[i : i + 2]
        if ch in " \t":
            end_word()
            i += 1
        elif ch in "\n\r":
            raise Unsupported("newline outside quotes")
        elif ch == "\\":
            if i + 1 >= n or command[i + 1] in "\n\r":
                raise Unsupported("backslash at the end of a line")
            mark_quoted()
            buf.append(command[i + 1])
            i += 2
        elif ch == "'":
            close = command.find("'", i + 1)
            if close < 0:
                raise Unsupported("unterminated single quote")
            mark_quoted()
            buf.append(command[i + 1 : close])
            i = close + 1
        elif ch == '"':
            mark_quoted()
            i = _read_double_quoted(command, i + 1, buf)
        elif pair in ("$(", "$'", '$"') or ch == "`":
            raise Unsupported(f"unsupported expansion {pair if ch == '$' else ch!r}")
        elif ch == "#" and not started:
            break
        elif ch in _OPERATOR_CHARS:
            if pair in ("((", "))"):
                raise Unsupported("arithmetic or nested group syntax")
            if pair == "<<":
                raise Unsupported("heredoc or herestring")
            op = next(o for o in _OPERATORS if command.startswith(o, i))
            if op in ("<", ">") and command[i + 1 : i + 2] == "(":
                raise Unsupported("process substitution")
            if op[0] in "<>" and quoted_from is None and _DIGITS.fullmatch("".join(buf)):
                buf, started = [], False
            end_word()
            tokens.append(_Op(op))
            i += len(op)
        else:
            started = True
            buf.append(ch)
            i += 1
    end_word()
    return tokens


def _read_double_quoted(command: str, i: int, buf: list[str]) -> int:
    """Append the body of a double-quoted string starting at `i`; return the index after the closing quote."""
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == '"':
            return i + 1
        if ch == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt in "\n\r":
                raise Unsupported("line continuation inside double quotes")
            buf.append(nxt if nxt in '$`"\\' else ch + nxt)
            i += 2
        elif command.startswith("$(", i) or ch == "`":
            raise Unsupported("command substitution")
        else:
            buf.append(ch)
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
                    if i + 1 >= len(tokens) or not isinstance(tokens[i + 1], _Word):
                        raise Unsupported("redirection without a target")
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
            target = words[1].text
            frame.cwd = target if frame.cwd is None else posixpath.join(frame.cwd, target)
            return
        frame.leading = False
        self.commands.extend(_unwrap(words, frame.cwd, self.depth))


def _unwrap(words: list[_Word], cwd: str | None, depth: int) -> list[SimpleCommand]:
    # Only an unquoted `NAME=` is an assignment; `"FOO=1" make` runs a program named `FOO=1`.
    while words and (m := _ASSIGNMENT.match(words[0].text)) and _unquoted_through(words[0], m.end()):
        words = words[1:]
    w = [word.text for word in words]
    if w[:1] == ["env"]:
        w.pop(0)
        while w:
            if w[0] in ("-i", "-", "--ignore-environment") or _ASSIGNMENT.match(w[0]):
                w.pop(0)
            elif w[0] == "-u" and len(w) > 1:
                del w[:2]
            elif w[0].startswith(("-u", "--unset=")):
                w.pop(0)
            else:
                break
    if w[:1] == ["time"]:
        w.pop(0)
        if w[:1] == ["-p"]:
            w.pop(0)
    if w[:1] == ["nohup"]:
        w.pop(0)
    if w[:1] == ["sudo"]:
        w.pop(0)
        if w[:1] == ["-u"] and len(w) > 1:
            del w[:2]
    if w[:2] == ["uv", "run"]:
        del w[:2]
    if len(w) > 2 and posixpath.basename(w[0]) in _PYTHONS and w[1] == "-m":
        return [_simple(w[2:], cwd)]
    if w[:1] == ["npx"]:
        w.pop(0)
    if depth == 0 and len(w) > 2 and posixpath.basename(w[0]) in _SHELLS and w[1] == "-c":
        inner = _parse(w[2], depth=1)
        if cwd is None:
            return list(inner)
        return [SimpleCommand(c.program, c.args, posixpath.join(cwd, c.cwd) if c.cwd else cwd) for c in inner]
    if not w:
        return []
    return [_simple(w, cwd)]


def _unquoted_through(word: _Word, end: int) -> bool:
    return word.quoted_from is None or word.quoted_from >= end


def _simple(w: list[str], cwd: str | None) -> SimpleCommand:
    return SimpleCommand(posixpath.basename(w[0].rstrip("/")) or w[0], tuple(w[1:]), cwd)
