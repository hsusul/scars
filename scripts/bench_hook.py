"""Full-hook latency, process start to exit, with 1,000 lessons, plus an import-time breakdown.

The benchmark itself is `scars bench` (see `scars.commands.bench`); this script adds `--importtime`,
which runs one hook under `python -X importtime` per case and prints the slowest imports, cumulative.

Usage: uv run python scripts/bench_hook.py [--runs N] [--concurrency N] [--cmd "python -m scars hook"]
       [--importtime]
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from scars import cli
from scars.commands.bench import ERROR, MISS, bench_env, build_home, payload

ENTRY = (sys.executable, "-X", "importtime", "-c", "from scars.hook_entry import main; main()")


def importtime(lessons: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home, repo = build_home(Path(tmp), lessons)
        env = bench_env(home)
        for name, error in (("no match", MISS), ("match", ERROR)):
            r = subprocess.run(
                [*ENTRY, "claude", "PostToolUseFailure"],
                input=payload(repo, f"imp-{name}", error),
                capture_output=True,
                text=True,
                env=env,
            )
            rows = []
            for line in r.stderr.splitlines():
                if line.startswith("import time:") and "|" in line and "cumulative" not in line:
                    own, cumulative, module = line[len("import time:") :].split("|")
                    rows.append((int(cumulative), int(own), module.rstrip()))
            total = sum(o for _c, o, _m in rows)
            print(f"\nimports, {name}: {len(rows)} modules, {total / 1000:.1f} ms self time in total")
            for cumulative, own, module in sorted(rows, reverse=True)[:25]:
                print(f"  {cumulative / 1000:6.1f} ms cumulative {own / 1000:6.1f} ms self  {module}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--lessons", type=int, default=1000)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--cmd", help="hook command before `<harness> <event>`")
    ap.add_argument("--importtime", action="store_true")
    args = ap.parse_args()
    argv = ["bench", "--runs", str(args.runs), "--lessons", str(args.lessons)]
    argv += ["--concurrency", str(args.concurrency), *(["--cmd", args.cmd] if args.cmd else [])]
    code = cli.main(argv)
    if code == 0 and args.importtime:
        importtime(args.lessons)
    sys.exit(code)


if __name__ == "__main__":
    main()
