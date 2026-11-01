"""Error-signature normalizer shared by the hook and the miner."""

import re

from scars.model import ErrorSignature

HEADER_PREFIX = "[scars "
"""How every rendered lesson starts. Text holding it, in any case, is never mined."""

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOISE = [
    (
        re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        "<uuid>",
    ),
    (re.compile(r"/Volumes/990 Pro"), "<vol>"),
    (re.compile(r"(?:~|\.{1,2})?(?:/[\w.\-@+]+)+/?"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]
_SKIP_LINE = re.compile(
    r"^(exit code (\d+|<n>)|error:?|traceback.*|-+|=+|\s*<n>?\s*|\s*file \".*\", line .*|\s*\^+\s*)$", re.I
)
_UNLESSONABLE = re.compile(
    r"timed out|hook error|^<tool_use_error>blocked|^blocked|the user doesn't want|interrupted"
    r"|denied by the claude code auto mode",
    re.I,
)
_USER_REJECTION = "The user doesn't want to proceed"
_ERROR_LINE = re.compile(
    r"^(?:[\w./+\-]+|\(eval\))(?:\[\d+\])?:"
    r"|\b(?:error|fatal|no such|not found|denied|invalid|unknown)\b"
    r"|\b[\w.]+(?:Error|Exception|Exit|Interrupt)\b",
    re.I,
)
_MAX = 120


def signature(text: str, *, command: str | None = None) -> ErrorSignature | None:
    """Normalize tool error output to a signature, or None when no lesson could address it.

    Output that quotes a rendered lesson is None, so a lesson cannot seed another from its own text.
    Prefer error-shaped lines to stdout. A compound or unsupported shell command needs such a line;
    a single command or non-shell tool keeps the first usable line as its fallback.
    """
    if text.startswith(_USER_REJECTION) or HEADER_PREFIX in text.lower():
        return None
    lines = [ln.strip() for ln in _ANSI.sub("", text).strip().splitlines()]
    if any(ln.startswith("Traceback") for ln in lines):
        exceptions = [ln for ln in lines if re.match(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b", ln)]
        lines = exceptions[-1:] or lines
    fallback = None
    for line in lines:
        normalized = line
        for pattern, replacement in _NOISE:
            normalized = pattern.sub(replacement, normalized)
        if _SKIP_LINE.match(normalized) or not re.search(r"[A-Za-z]{3,}", normalized):
            continue
        result = normalized[:_MAX].strip()
        if _ERROR_LINE.search(line):
            return None if _UNLESSONABLE.search(result) else ErrorSignature(result)
        if fallback is None:
            fallback = result
    if command is not None and re.search(r"[;&|\n()<>`]", command):
        from scars.shell import Unsupported, parse

        try:
            commands = parse(command, null_commands=True)
        except Unsupported:
            return None
        if len(commands) > 1 or any(c.cwd is not None or not c.cwd_known for c in commands):
            return None
    return None if fallback is None or _UNLESSONABLE.search(fallback) else ErrorSignature(fallback)


TOOL_PREFIX = "[tool "


def tool_signature(tool: str, sig: ErrorSignature) -> ErrorSignature:
    """`sig` qualified by the non-shell tool that failed, as `[tool <name>] <sig>`.

    A structured tool lesson's error trigger is this text, so it fires only when the same tool fails
    with the same signature; the hook and the outcome pass build it the same way for a failed non-shell
    call. A shell failure is never qualified.
    """
    return ErrorSignature(f"{TOOL_PREFIX}{tool}] {sig}")
