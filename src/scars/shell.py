"""Bounded shell parser for command triggers. See ARCHITECTURE "Matching", "Shell parsing".

The parser never guesses. Syntax it cannot model (substitutions, heredocs, multi-line input, shlex errors,
unknown operators) raises `Unsupported`, and the caller skips command triggers for that call (R12).

Known limits, chosen over a hand-written tokenizer:
- shlex does not report quoting, so a quoted argument made only of operator characters (`echo "&&"`) is
  read as the operator.
- shlex does not report spacing, so an all-digit token right before a redirection is taken as a file
  descriptor (`2>&1`), which also drops the argument in `echo 2 > f`.
"""

import posixpath
import re
import shlex
from dataclasses import dataclass

MAX_COMMAND_BYTES = 4096


@dataclass(frozen=True)
class SimpleCommand:
    program: str
    args: tuple[str, ...]
    cwd: str | None


class Unsupported(Exception):
    """The command uses syntax the parser does not handle, so command triggers are skipped."""


_REDIRECTS = frozenset({">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<>"})
# Longest first, so a punctuation run like `)|` or `>&` splits into the right operators.
_OPERATORS = ("&>>", "&&", "||", ">>", ">&", "<&", "&>", ">|", "<>", ";", "|", "&", "(", ")", "<", ">")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = frozenset({"bash", "sh"})
_PYTHONS = frozenset({"python", "python3"})


@dataclass(frozen=True)
class _Op:
    text: str


def parse(command: str) -> tuple[SimpleCommand, ...]:
    """Split a shell command into simple commands with wrappers removed.

    Raises `Unsupported` for input over 4 KB, `$(`, backticks, `<<`, a newline outside quotes, or any
    shlex error. `bash -c` and `sh -c` are parsed again one level deep. Wrappers are removed in one pass
    in the spec order, each at most once, so `nohup time make` leaves `time` as the program.
    """
    return _parse(command, depth=0)


def _parse(command: str, depth: int) -> tuple[SimpleCommand, ...]:
    if len(command.encode()) > MAX_COMMAND_BYTES:
        raise Unsupported("command longer than 4 KB")
    tokens = _tokenize(_strip_comment(command))
    commands: list[SimpleCommand] = []
    cwd: str | None = None
    # A cd inside a subshell does not reach commands after the closing paren, so keep cd as a command.
    leading = not any(isinstance(t, _Op) and t.text in ("(", ")") for t in tokens)
    for words, separator in _split(tokens):
        if leading and separator == "&&" and len(words) == 2 and words[0] == "cd":
            cwd = words[1] if cwd is None else posixpath.join(cwd, words[1])
            continue
        leading = False
        commands.extend(_unwrap(words, cwd, depth))
    return tuple(commands)


def _strip_comment(command: str) -> str:
    """Reject unsupported forms and cut an unquoted `#` comment, scanning with quote state."""
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        pair = command[i : i + 2]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
            elif pair == "$(" or ch == "`":
                raise Unsupported("command substitution")
        elif ch == "\\":
            if command[i + 1 : i + 2] in ("\n", "\r"):
                raise Unsupported("newline outside quotes")
            i += 1
        elif ch in "'\"":
            quote = ch
        elif pair == "$(" or ch == "`":
            raise Unsupported("command substitution")
        elif pair == "<<":
            raise Unsupported("heredoc or herestring")
        elif ch in "\n\r":
            raise Unsupported("newline outside quotes")
        elif ch == "#" and (i == 0 or command[i - 1].isspace() or command[i - 1] in ";&|()<>"):
            return command[:i]
        i += 1
    return command


def _tokenize(command: str) -> list[str | _Op]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # Comments are cut by _strip_comment; shlex would also cut `--format=#%h` mid-word.
    lexer.commenters = ""
    try:
        raw = list(lexer)
    except ValueError as exc:
        raise Unsupported(str(exc)) from exc
    tokens: list[str | _Op] = []
    for token in raw:
        if token and all(ch in lexer.punctuation_chars for ch in token):
            tokens.extend(_split_operators(token))
        else:
            tokens.append(token)
    return tokens


def _split_operators(run: str) -> list[_Op]:
    ops: list[_Op] = []
    while run:
        op = next((o for o in _OPERATORS if run.startswith(o)), None)
        if op is None:
            raise Unsupported(f"unknown operator {run!r}")
        ops.append(_Op(op))
        run = run[len(op) :]
    return ops


def _split(tokens: list[str | _Op]) -> list[tuple[list[str], str | None]]:
    """Group tokens into (words, separator that follows) runs, dropping redirections and targets."""
    runs: list[tuple[list[str], str | None]] = []
    words: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if isinstance(token, str):
            words.append(token)
        elif token.text in _REDIRECTS:
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if not isinstance(target, str):
                raise Unsupported("redirection without a target")
            if words and words[-1].isdigit():
                words.pop()
            i += 1
        else:
            if words:
                runs.append((words, token.text))
            words = []
        i += 1
    if words:
        runs.append((words, None))
    return runs


def _unwrap(words: list[str], cwd: str | None, depth: int) -> list[SimpleCommand]:
    w = list(words)
    while w and _ASSIGNMENT.match(w[0]):
        w.pop(0)
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
        del w[:2]
    if w[:1] == ["npx"]:
        w.pop(0)
    if depth == 0 and len(w) > 2 and posixpath.basename(w[0]) in _SHELLS and w[1] == "-c":
        inner = _parse(w[2], depth=1)
        if cwd is None:
            return list(inner)
        return [SimpleCommand(c.program, c.args, posixpath.join(cwd, c.cwd) if c.cwd else cwd) for c in inner]
    if not w:
        return []
    return [SimpleCommand(posixpath.basename(w[0].rstrip("/")) or w[0], tuple(w[1:]), cwd)]
