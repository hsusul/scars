"""`scars bench`: full-hook latency, process start to exit, with 1,000 lessons.

It builds a throwaway scars home with lessons that never match plus one that does, then runs the
installed `scars-hook` in fresh processes for a no-match failure and a matching failure, each run in a
new session so deduplication never hides the match. Each case runs sequentially and then with N
processes at once (16 by default), and prints p50, p95 and p99. Every run includes the telemetry write.
"""

from __future__ import annotations

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scars import render, store
from scars.commands import Context, UserError
from scars.model import EventType, LessonText, RepoId, RepoScope, Stage, Trigger, TriggerKind, TrustClass

REPO = RepoId("r-bench")
SIGNATURE = "Error: Cannot find module 'express'"
ERROR = "Exit code 1\n" + SIGNATURE
MISS = "Exit code 1\nError: nothing like any lesson"
WARMUP_RUNS = 5
FAILED = "npm test"
PASSED = "npm install"


def build_home(root: Path, lessons: int) -> tuple[Path, Path]:
    """A scars home under `root` with `lessons` provisional repair lessons, and a registered git repo."""
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
    with store.transaction(db):
        store.add_repo(db, REPO, "bench", "own")
        store.add_clone(db, common, REPO, 0.0)
        for i in range(lessons):
            signature = SIGNATURE if i == lessons - 1 else f"Error: bulk failure number {i}"
            lesson_id = store.create_lesson(
                db,
                text=LessonText(FAILED, f"run `{PASSED}` instead", render.MINED_REASON),
                scope=RepoScope(REPO),
                trust_class=TrustClass.OWN_CODE,
                triggers=(Trigger(TriggerKind.ERROR, signature, Stage.REPAIR),),
                fingerprint=f"fp{i}",
                actor="miner",
            )
            db.execute(
                "insert into observations (lesson_id, harness, session_id, failed_tool_use_id, failed_input, "
                "error_signature, passing_input, at) values (?,?,?,?,?,?,?,?)",
                (lesson_id, "claude", "old", f"tu{i}", FAILED, signature, PASSED, 0.0),
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


def one_run(cmd: list[str], env: dict[str, str], repo: Path, error: str, session: str) -> float:
    data = payload(repo, session, error)
    started = time.perf_counter()
    r = subprocess.run(
        [*cmd, "claude", "PostToolUseFailure"], input=data, capture_output=True, text=True, env=env
    )
    elapsed = (time.perf_counter() - started) * 1000
    if r.returncode != 0 or r.stderr:
        raise UserError(f"the hook failed: exit {r.returncode} {r.stderr[:200]!r}")
    if (error == ERROR) != bool(r.stdout):
        raise UserError(f"unexpected hook output for session {session}: {r.stdout[:200]!r}")
    return elapsed


def sample(
    cmd: list[str], env: dict[str, str], repo: Path, error: str, runs: int, tag: str, concurrency: int = 1
) -> list[float]:
    """Latencies of `runs` hook processes, `concurrency` at a time, sorted."""
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        times = list(pool.map(lambda i: one_run(cmd, env, repo, error, f"{tag}-{i}"), range(runs)))
    return sorted(times)


def pct(times: list[float], p: float) -> float:
    return times[min(len(times) - 1, int(len(times) * p))]


def line(label: str, times: list[float]) -> str:
    return (
        f"{label:<26} p50 {statistics.median(times):6.1f} ms  p95 {pct(times, 0.95):6.1f} ms  "
        f"p99 {pct(times, 0.99):6.1f} ms"
    )


def bench_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCARS_")}
    env["SCARS_HOME"] = str(home)
    return env


def cmd_bench(args: argparse.Namespace, ctx: Context) -> int:
    if args.runs < 1 or args.lessons < 1 or args.concurrency < 1:
        raise UserError("--runs, --lessons and --concurrency must be at least 1")
    cmd = shlex.split(args.cmd) if args.cmd else default_cmd()
    with tempfile.TemporaryDirectory(prefix="scars-bench-") as tmp:
        home, repo = build_home(Path(tmp), args.lessons)
        env = bench_env(home)
        sample(cmd, env, repo, MISS, WARMUP_RUNS, "warm")
        ctx.print(f"hook: {shlex.join(cmd)}")
        ctx.print(f"lessons: {args.lessons}  runs per case: {args.runs}")
        for label, n in (("sequential", 1), (f"{args.concurrency} concurrent", args.concurrency)):
            for name, error in (("no match", MISS), ("match", ERROR)):
                tag = f"{name.replace(' ', '')}-{n}"
                ctx.print(line(f"{label}, {name}", sample(cmd, env, repo, error, args.runs, tag, n)))
    return 0
