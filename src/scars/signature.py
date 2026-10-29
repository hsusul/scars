"""Error-signature normalizer shared by the hook and the miner. See ARCHITECTURE "Miner", step 4."""

import re

from scars.model import ErrorSignature

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
_MAX = 120


def signature(text: str) -> ErrorSignature | None:
    """Normalize tool error output to a signature, or None when no lesson could address it."""
    if text.startswith(_USER_REJECTION):
        return None
    lines = [ln.strip() for ln in _ANSI.sub("", text).strip().splitlines()]
    if any(ln.startswith("Traceback") for ln in lines):
        exceptions = [ln for ln in lines if re.match(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b", ln)]
        lines = exceptions[-1:] or lines
    for line in lines:
        normalized = line
        for pattern, replacement in _NOISE:
            normalized = pattern.sub(replacement, normalized)
        if _SKIP_LINE.match(normalized) or not re.search(r"[A-Za-z]{3,}", normalized):
            continue
        result = normalized.strip()[:_MAX]
        return None if _UNLESSONABLE.search(result) else ErrorSignature(result)
    return None
