"""`scars learn --dry-run`: show what the model would do with the best waiting candidates.

It takes candidates seen in 2 or more sessions, best first by the inbox score, at most
`MAX_PER_RUN`, and prints one verdict each. It opens lessons.db read-only and writes nothing. When
claude cannot answer at all (not installed, logged out, timed out) it says so once and stops, since
every other candidate would get the same answer.
"""

from __future__ import annotations

import argparse

from scars import learn
from scars.commands import Context, lessons_db, shown
from scars.commands.inbox import rank
from scars.model import Status

MAX_PER_RUN = 5
MIN_SESSIONS = 2


def _lines(lesson_id: str, verdict: learn.Verdict) -> list[str]:
    match verdict:
        case learn.Keep(text):
            return [
                f"{lesson_id}  keep  avoid: {text.avoid}",
                f"  instead: {text.instead}",
                f"  reason: {text.reason}",
            ]
        case learn.Reject(why):
            return [f"{lesson_id}  reject  {shown(why)}"]
        case learn.Unavailable(reason):
            return [f"{lesson_id}  unavailable  {reason}"]


def cmd_learn(args: argparse.Namespace, ctx: Context) -> int:
    if not args.dry_run:
        print("scars learn: only --dry-run is available", file=ctx.stderr)
        return 2
    with lessons_db(readonly=True) as db:
        picked = [
            r.lesson
            for r in rank(db, ctx.clock())
            if r.lesson.status is Status.CANDIDATE and r.lesson.sessions_seen >= MIN_SESSIONS
        ][:MAX_PER_RUN]
        records = [(lesson.id, learn.record(db, lesson)) for lesson in picked]
    if not records:
        ctx.print(f"no candidates seen in {MIN_SESSIONS} or more sessions")
        return 0
    for lesson_id, rec in records:
        verdict = learn.Unavailable("no observation recorded") if rec is None else ctx.judge(rec)
        if isinstance(verdict, learn.Unavailable) and verdict.reason in learn.CLAUDE_DOWN:
            ctx.print(f"claude unavailable: {verdict.reason}")
            return 0
        for line in _lines(lesson_id, verdict):
            ctx.print(line)
    return 0
