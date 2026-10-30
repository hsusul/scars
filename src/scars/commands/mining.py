"""`scars mine`: one miner batch over the default Claude Code and Codex transcript folders.

`--since` limits the batch to transcript files modified at or after that time. Each file still resumes
from its saved offset, so a later run without `--since` reads the rest and nothing is mined twice.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from scars import mine, paths, store
from scars.commands import Context, UserError
from scars.mine import Source, Summary

_AGO = re.compile(r"^(\d+)([dh])$")


def parse_since(text: str, now: float) -> float:
    """`30d`, `12h` or an ISO date or time, as a Unix time."""
    if m := _AGO.match(text):
        return now - int(m[1]) * (86400 if m[2] == "d" else 3600)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise UserError(f"--since takes 30d, 12h or a date like 2026-10-01, not {text!r}") from None


def recent_sources(sources: tuple[Source, ...], since: float) -> tuple[Source, ...]:
    """One source per transcript file modified at or after `since`, keeping each file's own path."""
    out = []
    for source in sources:
        if not source.root.is_dir():
            continue
        for path in sorted(source.root.glob(source.pattern)):
            try:
                if path.stat().st_mtime >= since:
                    out.append(Source(source.harness, path.parent, path.name))
            except OSError:
                continue
    return tuple(out)


def summary_lines(s: Summary) -> list[str]:
    lines = [
        f"read {s.sessions_read} transcripts: {s.calls} calls, {s.failures} failures, {s.pairs} pairs",
        f"lessons: {s.candidates_created} new, {s.observations} observations, "
        f"{s.provisionalized} provisional, {s.expired} expired",
    ]
    if s.pre_triggers_dropped:
        lines.append(f"pre-triggers dropped by the precision check: {s.pre_triggers_dropped}")
    if s.skipped:
        lines.append("skipped: " + ", ".join(f"{k} {v}" for k, v in s.skipped.items()))
    if s.rejected:
        lines.append("rejected: " + ", ".join(f"{k} {v}" for k, v in s.rejected.items()))
    if s.malformed_lines:
        lines.append(f"malformed lines: {s.malformed_lines}")
    if s.outcomes_written or s.models_backfilled:
        lines.append(f"outcomes written: {s.outcomes_written}, models backfilled: {s.models_backfilled}")
    return lines


def run(ctx: Context, since: float | None = None) -> Summary:
    sources = mine.default_sources(ctx.user_home)
    if since is not None:
        sources = recent_sources(sources, since)
    return mine.run(paths.home(), sources, ctx.clock())


def waiting_line(home: Path) -> str | None:
    db = store.connect(home / "lessons.db", readonly=True)
    try:
        waiting = store.candidate_count(db)
    finally:
        db.close()
    if not waiting:
        return None
    return f"{waiting} lesson{'s' if waiting != 1 else ''} waiting: run scars inbox"


def cmd_mine(args: argparse.Namespace, ctx: Context) -> int:
    since = parse_since(args.since, ctx.clock()) if args.since else None
    summary = run(ctx, since)
    for line in summary_lines(summary):
        ctx.print(line)
    if (line := waiting_line(paths.home())) is not None:
        ctx.print(line)
    return 0
