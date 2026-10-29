"""Recurring tool errors across Claude Code sessions, with timing and sensitivity analysis.

Read-only. For each (project, error signature) seen in 2+ sessions, orders occurrences by time and asks
whether a lesson created from the first occurrence could have reached each later session under three
lesson-latency models:
  live        lesson usable from the moment of the first occurrence
  session_end lesson usable once the first session ends (a session-end miner)
  next_day    lesson usable 24h after the first session ends (daily human review)

Signature variants (sensitivity):
  v1        the original normalization from mine_repeats.py
  v2        fixed normalization: ANSI stripped, UUIDs first, paths without spaces, last line of tracebacks
  v2_lesson v2 minus classes no lesson can fix: timeouts, hook denials, content-free lines

Usage: python3 mine_repeats_v2.py [projects_dir]
"""
import collections
import datetime as dt
import json
import os
import re
import sys

root = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/.claude/projects")

V1_NOISE = [(re.compile(r"/[\w.\-/ ]+"), "<path>"), (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
            (re.compile(r"\d+"), "<n>"), (re.compile(r"\s+"), " ")]
V1_GENERIC = re.compile(r"^(exit code \d+|error:?|traceback.*|\s*)$", re.I)

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
V2_NOISE = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"/Volumes/990 Pro"), "<vol>"),
    (re.compile(r"(?:~|\.{1,2})?(?:/[\w.\-@+]+)+/?"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]
V2_SKIP_LINE = re.compile(r"^(exit code (\d+|<n>)|error:?|traceback.*|-+|=+|\s*<n>?\s*|\s*file \".*\", line .*|\s*\^+\s*)$", re.I)
UNLESSONABLE = re.compile(r"timed out|hook error|^<tool_use_error>blocked|^blocked|the user doesn't want|interrupted", re.I)
USER_REJECTION = "The user doesn't want to proceed"


def sig_v1(text):
    if text.startswith(USER_REJECTION):
        return ""
    lines = [ln for ln in text.strip().splitlines() if not V1_GENERIC.match(ln.strip())]
    s = lines[0] if lines else ""
    for pat, rep in V1_NOISE:
        s = pat.sub(rep, s)
    return s[:120]


def sig_v2(text):
    if text.startswith(USER_REJECTION):
        return ""
    text = ANSI.sub("", text)
    lines = [ln.strip() for ln in text.strip().splitlines()]
    if any(ln.startswith("Traceback") for ln in lines):
        exc = [ln for ln in lines if re.match(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b", ln)]
        lines = exc[-1:] or lines
    out = ""
    for ln in lines:
        s = ln
        for pat, rep in V2_NOISE:
            s = pat.sub(rep, s)
        if not V2_SKIP_LINE.match(s) and re.search(r"[A-Za-z]{3,}", s):
            out = s.strip()
            break
    return out[:120]


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


occ = {"v1": collections.defaultdict(list), "v2": collections.defaultdict(list), "v2_lesson": collections.defaultdict(list)}
span = {}
seen_tool_use = set()
sessions = errors = copied = 0
for project in os.listdir(root):
    pdir = os.path.join(root, project)
    if not os.path.isdir(pdir):
        continue
    for name in os.listdir(pdir):
        if not name.endswith(".jsonl"):
            continue
        sessions += 1
        first = last = None
        with open(os.path.join(pdir, name), errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("timestamp")
                if t:
                    try:
                        tv = ts(t)
                    except ValueError:
                        continue
                    first = tv if first is None else min(first, tv)
                    last = tv if last is None else max(last, tv)
                if '"is_error":true' not in line and '"is_error": true' not in line:
                    continue
                msg = rec.get("message", {})
                for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") and t):
                        continue
                    tuid = block.get("tool_use_id")
                    if tuid in seen_tool_use:
                        copied += 1
                        continue
                    seen_tool_use.add(tuid)
                    errors += 1
                    text = result_text(block)
                    s1, s2 = sig_v1(text), sig_v2(text)
                    if s1:
                        occ["v1"][(project, s1)].append((tv, name))
                    if s2:
                        occ["v2"][(project, s2)].append((tv, name))
                        if not UNLESSONABLE.search(s2):
                            occ["v2_lesson"][(project, s2)].append((tv, name))
        if first is not None:
            span[name] = (first, last)

DAY = 86400.0


def analyze(table):
    recurring = 0
    repeats = 0
    preventable = {"live": 0, "session_end": 0, "next_day": 0}
    concurrent = 0
    for (_, _sig), hits in table.items():
        by_session = {}
        for tv, s in sorted(hits):
            by_session.setdefault(s, tv)
        if len(by_session) < 2:
            continue
        recurring += 1
        ordered = sorted((tv, s) for s, tv in by_session.items())
        t0, s0 = ordered[0]
        e0 = span.get(s0, (t0, t0))[1]
        for tv, s in ordered[1:]:
            repeats += 1
            if tv < e0:
                concurrent += 1
            if tv > t0:
                preventable["live"] += 1
            if tv > e0:
                preventable["session_end"] += 1
            if tv > e0 + DAY:
                preventable["next_day"] += 1
    return {"recurring_signatures": recurring, "repeat_occurrences": repeats,
            "repeats_while_first_session_still_running": concurrent,
            "reachable_if_lesson_live": preventable["live"],
            "reachable_if_lesson_at_session_end": preventable["session_end"],
            "reachable_if_lesson_next_day": preventable["next_day"]}


report = {"sessions_scanned": sessions, "tool_errors_unique": errors, "tool_errors_copied_from_resumed_sessions": copied}
for variant, table in occ.items():
    report[variant] = analyze(table)
top = sorted(((len({s for _, s in v}), k[1]) for k, v in occ["v2_lesson"].items()), reverse=True)[:25]
report["v2_lesson_top"] = [f"{n} sessions: {sig[:90]}" for n, sig in top if n >= 2]
print(json.dumps(report, indent=1))
