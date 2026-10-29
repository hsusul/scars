"""The v2 error-signature normalizer shared by the miners (ARCHITECTURE "Miner", step 4)."""
import re

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NOISE = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"/Volumes/990 Pro"), "<vol>"),
    (re.compile(r"(?:~|\.{1,2})?(?:/[\w.\-@+]+)+/?"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]
SKIP_LINE = re.compile(r"^(exit code (\d+|<n>)|error:?|traceback.*|-+|=+|\s*<n>?\s*|\s*file \".*\", line .*|\s*\^+\s*)$", re.I)
UNLESSONABLE = re.compile(r"timed out|hook error|^<tool_use_error>blocked|^blocked|the user doesn't want|interrupted|denied by the claude code auto mode", re.I)
USER_REJECTION = "The user doesn't want to proceed"


def sig_v2(text):
    if text.startswith(USER_REJECTION):
        return ""
    text = ANSI.sub("", text)
    lines = [ln.strip() for ln in text.strip().splitlines()]
    if any(ln.startswith("Traceback") for ln in lines):
        exc = [ln for ln in lines if re.match(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b", ln)]
        lines = exc[-1:] or lines
    for ln in lines:
        s = ln
        for pat, rep in NOISE:
            s = pat.sub(rep, s)
        if not SKIP_LINE.match(s) and re.search(r"[A-Za-z]{3,}", s):
            return s.strip()[:120]
    return ""
