"""Measure what concurrent agent sessions do to a shared lessons file.

Two write strategies, N processes each:
  append  - one os.write() per JSONL record on an O_APPEND fd (the "shared log" design)
  rmw     - read whole JSON array, append, write back (what single-file memory servers do)

Usage: python3 append_race.py <dir> [writers] [records_per_writer]
"""
import json
import multiprocessing as mp
import os
import sys
import time

SIZES = [256, 4096, 65536, 1048576]


def append_writer(path, wid, n, size):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    for seq in range(n):
        head = json.dumps({"w": wid, "s": seq, "pad": ""})[:-2]
        pad = "x" * max(0, size - len(head) - 3)
        line = (head + pad + '"}\n').encode()
        written = os.write(fd, line)
        assert written == len(line), f"short write {written} of {len(line)}"
    os.close(fd)


def rmw_writer(path, wid, n):
    for seq in range(n):
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append({"w": wid, "s": seq})
        with open(path, "w") as f:
            json.dump(data, f)


def check_append(path, writers, n):
    good, bad = 0, 0
    seen = set()
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
                seen.add((rec["w"], rec["s"]))
                good += 1
            except (ValueError, KeyError):
                bad += 1
    return good, bad, writers * n - len(seen)


def run(fn, args_for):
    procs = [mp.Process(target=fn, args=args_for(w)) for w in range(WRITERS)]
    t = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    return time.time() - t


if __name__ == "__main__":
    base = sys.argv[1]
    WRITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    N = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    os.makedirs(base, exist_ok=True)
    results = {"dir": base, "writers": WRITERS, "records_per_writer": N, "append": [], "rmw": None}

    for size in SIZES:
        n = N if size < 65536 else max(10, N // 10)
        path = os.path.join(base, f"append-{size}.jsonl")
        if os.path.exists(path):
            os.remove(path)
        secs = run(append_writer, lambda w: (path, w, n, size))
        good, bad, missing = check_append(path, WRITERS, n)
        results["append"].append({"record_bytes": size, "expected": WRITERS * n, "parsed": good,
                                  "corrupt_lines": bad, "missing": missing, "secs": round(secs, 2)})
        os.remove(path)

    path = os.path.join(base, "rmw.json")
    if os.path.exists(path):
        os.remove(path)
    rmw_n = 50
    secs = run(rmw_writer, lambda w: (path, w, rmw_n))
    try:
        with open(path) as f:
            kept = len(json.load(f))
        corrupt = False
    except json.JSONDecodeError:
        kept, corrupt = 0, True
    results["rmw"] = {"expected": WRITERS * rmw_n, "kept": kept, "file_corrupt": corrupt, "secs": round(secs, 2)}
    os.remove(path)
    print(json.dumps(results, indent=1))
