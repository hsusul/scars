"""Transcript miner: incremental readers, the pair rule, lesson writes, policy and the outcome pass.

`run` mines one batch:

1. Read the new complete lines of every transcript from its saved offset (from 0 when the file was
   replaced or shrank), into one ordered call stream per session.
2. Drop calls whose tool use id an earlier batch already processed: resumed sessions copy history.
3. Pair failures with their fixes (see `pairs`), continuing the failures left open by earlier batches.
4. Build each pair's candidate (see `build`), in order of the fixing call's time. A new fingerprint
   creates a candidate, which an `own_code` repo-scope candidate leaves at once as provisional; an
   existing one gets an `observed` event. Each pair adds one observation.
5. Commit offsets, processed ids, the open failures and the new mined calls in one transaction.
6. Expire lessons not sighted for 14 days, then write delayed hit outcomes.

Event times are the fixing call's time, so expiry counts from the real sighting. Mining the same
transcripts again changes nothing: offsets, processed ids and observations each guard the work.
"""

import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scars import hook, policy, render, store
from scars.match import RecordedCall, precision
from scars.mine import build, calls, claude, codex, outcomes, pairs
from scars.model import EventType, Harness, RepoId

DAY = 86400.0


@dataclass(frozen=True)
class Source:
    harness: Harness
    root: Path
    pattern: str


def default_sources(user_home: Path) -> tuple[Source, ...]:
    return (
        Source(Harness.CLAUDE, user_home / ".claude" / "projects", "*/*.jsonl"),
        Source(Harness.CODEX, user_home / ".codex" / "sessions", "**/rollout-*.jsonl"),
    )


@dataclass(frozen=True)
class Summary:
    sessions_read: int = 0
    """Transcript files with new complete lines."""
    calls: int = 0
    failures: int = 0
    pairs: int = 0
    candidates_created: int = 0
    observations: int = 0
    provisionalized: int = 0
    pre_triggers_dropped: int = 0
    expired: int = 0
    outcomes_written: int = 0
    models_backfilled: int = 0
    malformed_lines: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    """Candidates dropped by a render rule, by rule name. Never the text."""
    skipped: dict[str, int] = field(default_factory=dict)
    """Pairs that yield no lesson for a structural reason, by reason."""


_PARSERS = {Harness.CLAUDE: claude.parse, Harness.CODEX: codex.parse}


def _session_id(source: Source, path: Path) -> str:
    if source.harness is Harness.CODEX:
        return codex.session_id_from_name(str(path))
    return path.stem


@dataclass(frozen=True)
class _Read:
    offsets: tuple[store.Offset, ...]
    streams: dict[tuple[str, str], list[calls.Call]]
    sessions: int
    malformed: int


def _read(sources: Iterable[Source], saved: dict[str, store.Offset]) -> _Read:
    offsets: list[store.Offset] = []
    streams: dict[tuple[str, str], list[calls.Call]] = {}
    sessions = malformed = 0
    for source in sources:
        if not source.root.is_dir():
            continue
        for path in sorted(source.root.glob(source.pattern)):
            key = str(path)
            try:
                state = calls.file_state(key)
            except OSError:
                continue
            prev = saved.get(key)
            if prev is not None and prev.inode == state.inode and prev.mtime == state.mtime:
                continue
            start = calls.start_offset(prev.offset if prev else None, prev.inode if prev else None, state)
            try:
                chunk = calls.read_from(key, start)
            except OSError:
                continue
            session_id = _session_id(source, path)
            parsed = _PARSERS[source.harness](chunk.lines, session_id)
            resume = chunk.end if parsed.resume_offset is None else parsed.resume_offset
            offsets.append(store.Offset(key, resume, state.inode, state.mtime))
            malformed += chunk.malformed
            if chunk.lines:
                sessions += 1
            if parsed.calls:
                streams.setdefault((str(source.harness), session_id), []).extend(parsed.calls)
    return _Read(tuple(offsets), streams, sessions, malformed)


def _mined_row(call: calls.Call) -> store.MinedCall:
    command = call.command
    if command is not None and build.secret_rule(command) is not None:
        command = None
    sig = pairs.signature(call.output) if call.status is calls.CallStatus.FAILED else None
    return store.MinedCall(
        tool_use_id=call.tool_use_id,
        harness=str(call.harness),
        session_id=call.session_id,
        at=call.at,
        tool=call.tool,
        tool_kind=str(call.tool_kind),
        command=command,
        paths=call.paths,
        status=str(call.status),
        error_signature=sig,
        cwd=call.cwd,
        model_id=call.model_id,
    )


def _recorded(row: store.MinedCall) -> RecordedCall:
    return outcomes.recorded(row)


def _open_from_store(p: store.PendingFailure) -> pairs.OpenFailure:
    return pairs.OpenFailure(
        tool_use_id=p.tool_use_id,
        harness=p.harness,
        session_id=p.session_id,
        tool=p.tool,
        program=p.program,
        input=p.input,
        signature=p.error_signature,  # type: ignore[arg-type]
        calls_since=p.calls_since,
        last_call_at=p.last_call_at,
    )


def _pending_row(f: pairs.OpenFailure) -> store.PendingFailure:
    return store.PendingFailure(
        f.tool_use_id,
        f.harness,
        f.session_id,
        f.tool,
        f.program,
        f.input,
        f.signature,
        f.calls_since,
        f.last_call_at,
    )


class _Repos:
    """Working directory to registered repo and owner class, resolved once per directory per run."""

    def __init__(self, db: sqlite3.Connection, common_dir_of: Callable[[str], str | None]) -> None:
        self._db = db
        self._common_dir_of = common_dir_of
        self._cache: dict[str, tuple[RepoId | None, str | None]] = {}

    def resolve(self, cwd: str) -> tuple[RepoId | None, str | None]:
        if cwd not in self._cache:
            common = self._common_dir_of(cwd) if cwd and os.path.isdir(cwd) else None
            repo = store.repo_for_common_dir(self._db, (common, os.path.realpath(common))) if common else None
            self._cache[cwd] = (repo, store.repo_owner_class(self._db, repo) if repo else None)
        return self._cache[cwd]


def _write(
    db: sqlite3.Connection,
    candidate: build.Candidate,
    pair: pairs.Pair,
    recent: Sequence[RecordedCall],
    counts: Counter[str],
) -> None:
    at = pair.passing.at
    existing = store.get_lesson_by_fingerprint(db, candidate.fingerprint)
    if existing is not None:
        last = store.lesson_last_seen(db, existing.id) or at
        revision = store.apply_event(
            db,
            lesson_id=existing.id,
            event=EventType.OBSERVED,
            expected_revision=existing.revision,
            actor=policy.ACTOR,
            payload={"fix_program": candidate.fix_program},
            at=max(at, last),
        )
        lesson_id = existing.id
        pre = next((t for t in existing.triggers if t.kind.value in ("command", "path")), None)
        checked = precision(pre, recent, error_signature=candidate.signature) if pre else None
    else:
        pre = candidate.pre_trigger
        checked = precision(pre, recent, error_signature=candidate.signature) if pre else None
        if checked is not None and not policy.keeps_pre_trigger(checked):
            counts["pre_triggers_dropped"] += 1
            pre = None
        lesson_id = store.create_lesson(
            db,
            text=candidate.text,
            scope=candidate.scope,
            trust_class=candidate.trust_class,
            triggers=(candidate.error_trigger, *([pre] if pre else [])),
            fingerprint=candidate.fingerprint,
            actor=policy.ACTOR,
            fix_program=candidate.fix_program,
            at=at,
        )
        counts["candidates_created"] += 1
        revision = 1
        if policy.provisional_on_first_sighting(candidate.scope, candidate.trust_class):
            revision = store.apply_event(
                db,
                lesson_id=lesson_id,
                event=EventType.PROVISIONALIZED,
                expected_revision=revision,
                actor=policy.ACTOR,
                payload={},
                at=at,
            )
            counts["provisionalized"] += 1
    if checked is not None:
        store.record_precision(
            db,
            lesson_id,
            revision,
            matched_failing=checked.matched_failing,
            matched_passing=checked.matched_passing,
            window_days=policy.PRECISION_WINDOW_DAYS,
            at=at,
        )
    added = store.add_observation(
        db,
        store.Observation(
            lesson_id=lesson_id,
            harness=pair.failed.harness,
            session_id=pair.failed.session_id,
            failed_tool_use_id=pair.failed.tool_use_id,
            failed_input=candidate.failed_input,
            error_signature=candidate.signature,
            passing_input=candidate.passing_input,
            at=at,
        ),
    )
    counts["observations"] += int(added)


def run(
    home: Path,
    sources: Iterable[Source],
    now: float,
    *,
    common_dir_of: Callable[[str], str | None] = hook.git_common_dir,
) -> Summary:
    """Mine one batch into `home`/lessons.db and write outcomes into `home`/telemetry.db if it exists."""
    db = store.init_lessons(home / "lessons.db")
    try:
        store.ensure_miner_schema(db)
        return _run(db, home, sources, now, common_dir_of)
    finally:
        db.close()


def _run(
    db: sqlite3.Connection,
    home: Path,
    sources: Iterable[Source],
    now: float,
    common_dir_of: Callable[[str], str | None],
) -> Summary:
    counts: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    read = _read(sources, store.miner_offsets(db))
    all_ids = [c.tool_use_id for stream in read.streams.values() for c in stream]
    seen = set(store.seen_tool_uses(db, all_ids))
    streams: dict[tuple[str, str], list[calls.Call]] = {}
    for key, stream in read.streams.items():
        for call in stream:
            if call.tool_use_id not in seen:
                seen.add(call.tool_use_id)
                streams.setdefault(key, []).append(call)
    new_calls = [c for stream in streams.values() for c in stream]
    waiting: dict[tuple[str, str], list[pairs.OpenFailure]] = {}
    for p in store.pending_failures(db):
        waiting.setdefault((p.harness, p.session_id), []).append(_open_from_store(p))
    found: list[pairs.Pair] = []
    still_open: list[pairs.OpenFailure] = []
    for key in sorted(set(streams) | set(waiting)):
        result = pairs.pair_session(waiting.get(key, ()), streams.get(key, ()), now)
        found.extend(result.pairs)
        still_open.extend(result.still_open)
        counts["failures"] += result.failures
    since = now - policy.PRECISION_WINDOW_DAYS * DAY
    rows = [_mined_row(c) for c in new_calls]
    recent = [_recorded(r) for r in store.mined_calls_since(db, since)]
    recent += [_recorded(r) for r in rows if r.at >= since]
    repos = _Repos(db, common_dir_of)
    for pair in sorted(found, key=lambda p: (p.passing.at, p.failed.tool_use_id)):
        if store.has_observation(db, pair.failed.tool_use_id):
            continue
        repo_id, owner_class = repos.resolve(pair.passing.cwd)
        try:
            candidate = build.build(pair, repo_id, owner_class)
        except build.Skipped as exc:
            skipped[exc.reason] += 1
            continue
        except render.RejectedText as exc:
            rejected[exc.rule] += 1
            continue
        try:
            _write(db, candidate, pair, recent, counts)
        except (store.Conflict, store.Rejected):
            skipped["store_conflict"] += 1
    store.commit_batch(
        db,
        offsets=read.offsets,
        seen=[c.tool_use_id for c in new_calls],
        pending=[_pending_row(f) for f in still_open],
        calls=[r for r in rows if r.at >= since],
        prune_before=since,
    )
    expired = policy.expire(db, now)
    result = outcomes.run(db, str(home / "telemetry.db"), now)
    return Summary(
        sessions_read=read.sessions,
        calls=len(new_calls),
        failures=counts["failures"],
        pairs=len(found),
        candidates_created=counts["candidates_created"],
        observations=counts["observations"],
        provisionalized=counts["provisionalized"],
        pre_triggers_dropped=counts["pre_triggers_dropped"],
        expired=expired,
        outcomes_written=result.outcomes,
        models_backfilled=result.models,
        malformed_lines=read.malformed,
        rejected=dict(sorted(rejected.items())),
        skipped=dict(sorted(skipped.items())),
    )


__all__ = ["Source", "Summary", "default_sources", "run"]
