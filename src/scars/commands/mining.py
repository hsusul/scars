"""`scars mine`: one miner batch over the default Claude Code and Codex transcript folders.

`--since` limits the batch to transcript files modified at or after that time. Each file still resumes
from its saved offset, so a later run without `--since` reads the rest and nothing is mined twice. The
sessions whose files were skipped keep their pending failures untouched; every session that was read
follows the normal rules.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from scars import mine, paths, review, store
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


def recent_sources(
    sources: tuple[Source, ...], since: float
) -> tuple[tuple[Source, ...], frozenset[tuple[str, str]]]:
    """One source per transcript file modified at or after `since`, keeping each file's own path, and
    the `(harness, session id)` keys of the files left out (minus any session that also has a file in)."""
    out = []
    kept: set[tuple[str, str]] = set()
    skipped: set[tuple[str, str]] = set()
    for source in sources:
        if not source.root.is_dir():
            continue
        for path in sorted(source.root.glob(source.pattern)):
            key = mine.session_key(source, path)
            try:
                recent = path.stat().st_mtime >= since
            except OSError:
                recent = False
            if recent:
                out.append(Source(source.harness, path.parent, path.name))
                kept.add(key)
            else:
                skipped.add(key)
    return tuple(out), frozenset(skipped - kept)


def summary_lines(s: Summary) -> list[str]:
    lines = [
        f"read {s.sessions_read} transcripts: {s.calls} calls, {s.failures} failures, {s.pairs} pairs",
        f"lessons: {s.candidates_created} new, {s.observations} observations, {s.expired} expired",
    ]
    if s.retired_unrenderable:
        rules = ", ".join(f"{k} {v}" for k, v in s.retired_unrenderable.items())
        lines.append(f"retired because they no longer render: {rules}")
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
    skipped: frozenset[tuple[str, str]] = frozenset()
    if since is not None:
        sources, skipped = recent_sources(sources, since)
    return mine.run(paths.home(), sources, ctx.clock(), skipped_sessions=skipped)


def _waiting(home: Path) -> review.Waiting:
    db = store.connect(home / "lessons.db", readonly=True)
    try:
        return review.waiting(db)
    finally:
        db.close()


def _count_line(found: review.Waiting) -> str | None:
    waiting = len(found.lessons)
    if not waiting:
        return None
    return f"{waiting} lesson{'s' if waiting != 1 else ''} waiting: run scars inbox"


def waiting_line(home: Path) -> str | None:
    return _count_line(_waiting(home))


def cmd_mine(args: argparse.Namespace, ctx: Context) -> int:
    since = parse_since(args.since, ctx.clock()) if args.since else None
    summary = run(ctx, since)
    for line in summary_lines(summary):
        ctx.print(line)
    found = _waiting(paths.home())
    for line in (_count_line(found), review.hidden_line(found)):
        if line is not None:
            ctx.print(line)
    return 0
