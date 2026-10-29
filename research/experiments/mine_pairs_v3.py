"""Count what the Phase 1 miner would actually keep: failure-then-fix pairs that recur across sessions.

Read-only. Applies the ARCHITECTURE "Miner" rules to Claude Code transcripts:
  - dedupe copied history by tool_use_id
  - v2 error signature (imported from mine_repeats_v2), excluding unlessonable classes
  - a pair = failed call, then the next successful call of the same tool (same program for Bash)
    within 10 tool calls in the same session, with no Edit/Write/MultiEdit/NotebookEdit in between
  - optional window: only sessions whose first record is within the last N days

For each (project, signature) kept as a pair in 2+ sessions it reports repeats after the first pair,
and when a lesson could exist under the ADR-0004 rule (second sighting + 2 h miner cadence).

Usage: python3 mine_pairs_v3.py [projects_dir] [--days N]
"""
import collections
import datetime as dt
import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signature import UNLESSONABLE, sig_v2  # noqa: E402

args = [a for a in sys.argv[1:]]
days = int(args[args.index("--days") + 1]) if "--days" in args else None
root = os.path.expanduser(next((a for a in args if not a.startswith("--") and not a.isdigit()), "~/.claude/projects"))
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
cutoff = time.time() - days * 86400 if days else 0


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def program(cmd):
    try:
        toks = shlex.split(cmd or "")
    except ValueError:
        toks = (cmd or "").split()
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("env", "time", "sudo")):
        toks = toks[1:]
    if toks[:2] == ["uv", "run"]:
        toks = toks[2:]
    return toks[0].rsplit("/", 1)[-1] if toks else ""


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


seen = set()
pairs = collections.defaultdict(list)
category = collections.Counter()
sessions_in_window = 0
for project in os.listdir(root):
    pdir = os.path.join(root, project)
    if not os.path.isdir(pdir):
        continue
    for name in os.listdir(pdir):
        if not name.endswith(".jsonl"):
            continue
        calls = {}
        seq = []
        first = None
        with open(os.path.join(pdir, name), errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("timestamp")
                if t and first is None:
                    try:
                        first = ts(t)
                    except ValueError:
                        pass
                content = rec.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        tool = b.get("name", "")
                        calls[b.get("id")] = (tool, program(inp.get("command")) if tool == "Bash" else tool)
                    elif b.get("type") == "tool_result" and t:
                        tuid = b.get("tool_use_id")
                        if tuid in seen:
                            continue
                        seen.add(tuid)
                        tool, prog = calls.get(tuid, ("?", "?"))
                        seq.append((ts(t), tool, prog, bool(b.get("is_error")), result_text(b) if b.get("is_error") else ""))
        if first is None or first < cutoff:
            continue
        sessions_in_window += 1
        for i, (tv, tool, prog, err, text) in enumerate(seq):
            if not err:
                continue
            sig = sig_v2(text)
            if not sig or UNLESSONABLE.search(sig):
                continue
            for tv2, tool2, prog2, err2, _ in seq[i + 1:i + 11]:
                if tool2 in EDIT_TOOLS:
                    break
                if tool2 == tool and prog2 == prog and not err2:
                    pairs[(project, sig)].append((tv, name, tv2))
                    category[tool if tool != "Bash" else f"Bash:{prog}"] += 1
                    break

HOUR = 3600.0
recurring = repeats = reachable = reachable_first = 0
top = []
for key, hits in pairs.items():
    by_session = {}
    for tv, s, fixed_at in sorted(hits):
        by_session.setdefault(s, (tv, fixed_at))
    if len(by_session) < 2:
        continue
    recurring += 1
    ordered = sorted(by_session.values())
    repeats += len(ordered) - 1
    second_fix = ordered[1][1] + 2 * HOUR
    reachable += sum(1 for tv, _ in ordered[2:] if tv > second_fix)
    first_fix = ordered[0][1] + 2 * HOUR
    reachable_first += sum(1 for tv, _ in ordered[1:] if tv > first_fix)
    top.append((len(ordered), key[1][:90]))

top.sort(reverse=True)
print(json.dumps({
    "window_days": days, "sessions_in_window": sessions_in_window,
    "pair_signatures_recurring": recurring,
    "pair_repeats_after_first": repeats,
    "reachable_under_adr0004_second_sighting_plus_2h": reachable,
    "reachable_if_shared_after_first_sighting_plus_2h": reachable_first,
    "pairs_by_tool": category.most_common(12),
    "top": [f"{n} sessions: {s}" for n, s in top[:15]],
}, indent=1))
