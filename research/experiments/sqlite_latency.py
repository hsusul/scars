"""Hook read latency from a single SQLite store while concurrent writers append telemetry.

Each sample is a fresh python process that opens the DB read-only, selects active lessons for a scope,
and regex-matches a command, which is what a PreToolUse hook would do. Writers insert hit rows in
BEGIN IMMEDIATE transactions the whole time.

Usage: python3 sqlite_latency.py <dir> [lessons] [writers] [samples]
"""
import json
import multiprocessing as mp
import os
import sqlite3
import statistics
import subprocess
import sys
import time

READER = r'''
import json, re, sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=2)
rows = db.execute("select id, pattern, avoid, instead from lessons where status in ('provisional','promoted') and scope in ('machine', ?)", (sys.argv[2],)).fetchall()
cmd = sys.argv[3]
print(json.dumps([r[0] for r in rows if re.search(r[1], cmd)][:3]))
'''


def setup(path, n):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("pragma journal_mode=wal")
    db.execute("create table lessons (id text primary key, scope text, status text, pattern text, avoid text, instead text)")
    db.execute("create index lessons_scope on lessons(scope, status)")
    db.execute("create table hits (seq integer primary key, lesson text, session text, at real, outcome text)")
    words = ["xcodebuild", "tox", "uv sync", "npm test", "pytest", "cargo build", "git push", "swift", "make", "docker"]
    db.executemany("insert into lessons values (?,?,?,?,?,?)",
                   [(f"L{i:05d}", "machine" if i % 5 == 0 else f"repo{i % 15}", "promoted" if i % 3 == 0 else "candidate",
                     rf"\b{words[i % len(words)]}\b.*--flag{i}", f"avoid {i}", f"instead {i}") for i in range(n)])
    db.commit()
    db.close()


def writer(path, stop_at):
    db = sqlite3.connect(path, timeout=5)
    db.execute("pragma busy_timeout=5000")
    i = 0
    while time.time() < stop_at:
        db.execute("begin immediate")
        db.execute("insert into hits (lesson, session, at, outcome) values (?,?,?,?)", (f"L{i % 100:05d}", str(os.getpid()), time.time(), "shown"))
        db.execute("commit")
        i += 1
    db.close()


def sample(path, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        subprocess.run([sys.executable, "-c", READER, path, "repo3", "xcodebuild -scheme App --flag30"], check=True, capture_output=True)
        out.append((time.perf_counter() - t) * 1000)
    out.sort()
    return {"p50_ms": round(statistics.median(out), 1), "p95_ms": round(out[int(len(out) * 0.95) - 1], 1),
            "p99_ms": round(out[max(0, int(len(out) * 0.99) - 1)], 1), "max_ms": round(out[-1], 1)}


if __name__ == "__main__":
    base = sys.argv[1]
    lessons = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    writers = int(sys.argv[3]) if len(sys.argv) > 3 else 16
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "store.sqlite3")
    setup(path, lessons)
    result = {"dir": base, "lessons": lessons, "idle": sample(path, n)}
    stop_at = time.time() + 60
    procs = [mp.Process(target=writer, args=(path, stop_at)) for _ in range(writers)]
    for p in procs:
        p.start()
    time.sleep(1)
    result[f"with_{writers}_writers"] = sample(path, n)
    for p in procs:
        p.terminate()
        p.join()
    db = sqlite3.connect(path)
    result["hit_rows_written"] = db.execute("select count(*) from hits").fetchone()[0]
    db.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    print(json.dumps(result, indent=1))
