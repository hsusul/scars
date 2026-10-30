"""Full-hook latency, process start to exit, with 1,000 lessons.

Builds a throwaway scars home with 999 repair lessons that never match and one that does, then runs the
hook in fresh processes for a no-match failure and a matching failure, each with a new session so dedupe
never hides the match. Prints p50, p95 and p99 per case. With `--importtime` it also runs one hook under
`python -X importtime` and prints the slowest imports, cumulative.

Usage: uv run python scripts/bench_hook.py [--runs N] [--cmd "python -m scars hook"] [--importtime]

The default command is the `scars-hook` console script next to this interpreter when it exists, otherwise
`python -m scars hook`.
"""

import argparse
import json
import os
import shlex
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scars import store
from scars.model import EventType, LessonText, RepoId, RepoScope, Stage, Trigger, TriggerKind, TrustClass

REPO = RepoId("r-bench")
SIGNATURE = "Error: Cannot find module 'express'"
ERROR = "Exit code 1\n" + SIGNATURE
MISS = "Exit code 1\nError: nothing like any lesson"


def build_home(root: Path, lessons: int) -> tuple[Path, Path]:
    home, repo = root / "home", root / "repo"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    db = store.init_lessons(home / "lessons.db")
    db.row_factory = sqlite3.Row
    store.add_repo(db, REPO, "bench", "own")
    db.execute("insert into clones (common_dir, repo_id, registered_at) values (?,?,?)", (common, REPO, 0.0))
    for i in range(lessons):
        reason = SIGNATURE if i == lessons - 1 else f"Error: bulk failure number {i}"
        lesson_id = store.create_lesson(
            db,
            text=LessonText("npm test", "run `npm install && npm test`", reason),
            scope=RepoScope(REPO),
            trust_class=TrustClass.OWN_CODE,
            triggers=(Trigger(TriggerKind.ERROR, reason, Stage.REPAIR),),
            fingerprint=f"fp{i}",
            actor="miner",
        )
        db.execute(
            "insert into observations (lesson_id, harness, session_id, failed_tool_use_id, failed_input, "
            "error_signature, passing_input, at) values (?,?,?,?,?,?,?,?)",
            (lesson_id, "claude", "old", f"tu{i}", "npm test", reason, "npm install && npm test", 0.0),
        )
        store.apply_event(
            db,
            lesson_id=lesson_id,
            event=EventType.PROVISIONALIZED,
            expected_revision=1,
            actor="miner",
            payload={},
        )
    db.close()
    store.init_telemetry(home / "telemetry.db").close()
    return home, repo


def payload(repo: Path, session: str, error: str) -> str:
    return json.dumps(
        {
            "session_id": session,
            "cwd": str(repo),
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_use_id": "tu-bench",
            "error": error,
        }
    )


def default_cmd() -> list[str]:
    script = Path(sys.executable).parent / "scars-hook"
    return [str(script)] if script.exists() else [sys.executable, "-m", "scars", "hook"]


def sample(cmd: list[str], env: dict[str, str], repo: Path, error: str, runs: int, tag: str) -> list[float]:
    times = []
    for i in range(runs):
        data = payload(repo, f"{tag}-{i}", error)
        t = time.perf_counter()
        r = subprocess.run(
            [*cmd, "claude", "PostToolUseFailure"], input=data, capture_output=True, text=True, env=env
        )
        times.append((time.perf_counter() - t) * 1000)
        if r.returncode != 0 or r.stderr:
            sys.exit(f"hook failed: {r.returncode} {r.stderr[:300]}")
        if (error == ERROR) != bool(r.stdout):
            sys.exit(f"unexpected output for {tag}: {r.stdout[:200]!r}")
    return sorted(times)


def pct(times: list[float], p: float) -> float:
    return times[min(len(times) - 1, int(len(times) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--lessons", type=int, default=1000)
    ap.add_argument("--cmd", help="hook command before `<harness> <event>`")
    ap.add_argument("--importtime", action="store_true")
    args = ap.parse_args()
    cmd = shlex.split(args.cmd) if args.cmd else default_cmd()
    with tempfile.TemporaryDirectory() as tmp:
        home, repo = build_home(Path(tmp), args.lessons)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCARS_")}
        env["SCARS_HOME"] = str(home)
        sample(cmd, env, repo, MISS, 5, "warm")
        print(f"command: {shlex.join(cmd)}  lessons: {args.lessons}  runs per case: {args.runs}")
        for name, error in (("no match", MISS), ("match", ERROR)):
            t = sample(cmd, env, repo, error, args.runs, name.replace(" ", ""))
            print(
                f"{name:9} p50 {statistics.median(t):6.1f} ms  p95 {pct(t, 0.95):6.1f} ms  "
                f"p99 {pct(t, 0.99):6.1f} ms"
            )
        if args.importtime:
            for name, error in (("no match", MISS), ("match", ERROR)):
                py = [sys.executable, "-X", "importtime", *cmd[1:]] if cmd[0] == sys.executable else None
                if py is None:
                    py = [
                        sys.executable,
                        "-X",
                        "importtime",
                        "-c",
                        "from scars.hook_entry import main; main()",
                    ]
                r = subprocess.run(
                    [*py, "claude", "PostToolUseFailure"],
                    input=payload(repo, f"imp-{name}", error),
                    capture_output=True,
                    text=True,
                    env=env,
                )
                rows = []
                for line in r.stderr.splitlines():
                    if line.startswith("import time:") and "|" in line and "cumulative" not in line:
                        _self, cumulative, module = line[len("import time:") :].split("|")
                        rows.append((int(cumulative), int(_self), module.rstrip()))
                total = sum(s for _c, s, _m in rows)
                print(f"\nimports, {name}: {len(rows)} modules, {total / 1000:.1f} ms self time in total")
                for cumulative, own, module in sorted(rows, reverse=True)[:25]:
                    print(f"  {cumulative / 1000:6.1f} ms cumulative {own / 1000:6.1f} ms self  {module}")


if __name__ == "__main__":
    main()
