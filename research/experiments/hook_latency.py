"""Measure end-to-end latency of a PreToolUse-style lesson matcher run as a fresh process.

Builds a synthetic store of per-writer JSONL event files, then times N subprocess runs of a
matcher that globs the store, reduces events to current lessons, and regex-matches a command.

Usage: python3 hook_latency.py <store_dir> [writers] [events_per_writer] [runs]
"""
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time

MATCHER = r'''
import glob, json, os, re, sys
store = sys.argv[1]
cmd = sys.argv[2]
state = {}
for path in glob.glob(os.path.join(store, "*.jsonl")):
    with open(path, "rb") as f:
        for raw in f:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev["type"] == "proposed":
                state.setdefault(ev["id"], {"ev": ev, "status": "candidate"})
            elif ev["id"] in state:
                state[ev["id"]]["status"] = {"verified": "verified", "promoted": "promoted", "retired": "retired"}.get(ev["type"], state[ev["id"]]["status"])
hits = [s["ev"]["claim"] for s in state.values()
        if s["status"] == "promoted" and s["ev"]["trigger"]["kind"] == "command"
        and re.search(s["ev"]["trigger"]["pattern"], cmd)]
print(json.dumps({"hits": hits[:3], "lessons": len(state)}))
'''


def build(store, writers, per_writer):
    os.makedirs(store, exist_ok=True)
    for f in os.listdir(store):
        os.remove(os.path.join(store, f))
    rng = random.Random(7)
    words = ["xcodebuild", "tox", "uv sync", "npm test", "pytest", "cargo build", "git push", "swift", "make", "docker"]
    for w in range(writers):
        with open(os.path.join(store, f"claude-{w:05d}.jsonl"), "w") as f:
            for e in range(per_writer):
                lid = f"{w:05d}{e:04d}"
                word = rng.choice(words)
                f.write(json.dumps({"type": "proposed", "id": lid, "claim": f"lesson about {word} #{lid}",
                                    "scope": {"kind": "machine"}, "trigger": {"kind": "command", "pattern": rf"\b{word}\b.*--flag{e}"},
                                    "evidence": {"kind": "human"}, "at": "2026-10-29T00:00:00Z", "by": f"claude-{w}"}) + "\n")
                if rng.random() < 0.3:
                    f.write(json.dumps({"type": "promoted", "id": lid, "enforcement": "hint", "at": "2026-10-29T00:00:01Z", "by": "human"}) + "\n")


def time_runs(store, runs, flags):
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run([sys.executable, *flags, "-c", MATCHER, store, "xcodebuild -scheme App --flag3"], check=True, capture_output=True)
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return {"p50_ms": round(statistics.median(samples), 1), "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 1), "max_ms": round(samples[-1], 1)}


if __name__ == "__main__":
    store = sys.argv[1]
    runs = int(sys.argv[4]) if len(sys.argv) > 4 else 40
    out = []
    for writers, per in [(0, 0), (50, 4), (500, 4), (2000, 4), (5000, 4)]:
        build(store, writers, per)
        row = {"writer_files": writers, "events": writers * per}
        row["python"] = time_runs(store, runs, [])
        row["python_-S_-E"] = time_runs(store, runs, ["-S", "-E"])
        out.append(row)
    base = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run([shutil.which("true")], check=True)
        base.append((time.perf_counter() - t) * 1000)
    out.append({"baseline_spawn_true_p50_ms": round(statistics.median(base), 1)})
    print(json.dumps(out, indent=1))
