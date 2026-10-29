"""Count tool errors that recur across separate Claude Code sessions of the same project.

Read-only. Streams ~/.claude/projects/*/*.jsonl, finds tool_result blocks with is_error=true,
normalizes the error text into a signature, and reports how many signatures show up in 2+ sessions.

Usage: python3 mine_repeats.py [projects_dir] [--top N]
"""
import collections
import hashlib
import json
import os
import re
import sys

root = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "~/.claude/projects")
top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 0

NOISE = [
    (re.compile(r"/[\w.\-/ ]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]


GENERIC = re.compile(r"^(exit code \d+|error:?|traceback.*|\s*)$", re.I)
USER_REJECTION = "The user doesn't want to proceed"


def signature(text):
    if text.startswith(USER_REJECTION):
        return ""
    lines = [ln for ln in text.strip().splitlines() if not GENERIC.match(ln.strip())]
    first = lines[0] if lines else ""
    for pat, rep in NOISE:
        first = pat.sub(rep, first)
    return first[:120]


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


sessions_per_sig = collections.defaultdict(set)
projects_per_sig = collections.defaultdict(set)
errors = sessions = 0
for project in os.listdir(root):
    pdir = os.path.join(root, project)
    if not os.path.isdir(pdir):
        continue
    for name in os.listdir(pdir):
        if not name.endswith(".jsonl"):
            continue
        sessions += 1
        with open(os.path.join(pdir, name), errors="replace") as f:
            for line in f:
                if '"is_error":true' not in line and '"is_error": true' not in line:
                    continue
                try:
                    msg = json.loads(line).get("message", {})
                except ValueError:
                    continue
                for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        errors += 1
                        sig = signature(result_text(block))
                        if sig:
                            sessions_per_sig[(project, sig)].add(name)
                            projects_per_sig[sig].add(project)

repeat = {k: v for k, v in sessions_per_sig.items() if len(v) >= 2}
repeat_occ = sum(len(v) for v in repeat.values())
report = {
    "sessions_scanned": sessions,
    "tool_errors": errors,
    "distinct_signatures_per_project": len(sessions_per_sig),
    "signatures_recurring_in_2plus_sessions": len(repeat),
    "session_occurrences_of_recurring_signatures": repeat_occ,
    "avoidable_repeats_if_first_occurrence_was_shared": repeat_occ - len(repeat),
    "signatures_seen_in_2plus_projects": sum(1 for v in projects_per_sig.values() if len(v) >= 2),
}
if top:
    ranked = sorted(repeat.items(), key=lambda kv: -len(kv[1]))[:top]
    report["top"] = [{"sessions": len(v), "sig": hashlib.sha1(k[1].encode()).hexdigest()[:8], "text": k[1][:100]} for k, v in ranked]
print(json.dumps(report, indent=1))
