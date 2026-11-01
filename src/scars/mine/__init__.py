"""Transcript miner: incremental readers, the pair rule, lesson writes, policy and the outcome pass.

`run` mines one batch:

1. Read the new complete lines of every transcript from its saved offset (from 0 when the file was
   replaced or shrank), into one call stream per session in invocation order.
2. Drop calls whose tool use id an earlier batch already processed: resumed sessions copy history.
3. Pair failures with their fixes (see `pairs`), continuing the failures left open by earlier batches.
4. Build each pair's candidate (see `build`), in order of the fixing call's time. A new fingerprint
   creates a candidate, which waits in the inbox for a human; an existing one gets an `observed` event
   the first time a session sights it, so `sessions_seen` counts sessions, not repeats within one.
   Each pair adds one observation. A pair's lesson write, precision run and observation commit in one
   transaction, and the observation, unique per failed tool use id, marks the pair processed, so a crash
   never counts a sighting twice.
5. Commit offsets, processed ids, the open failures and the new mined calls in one transaction.

Before anything else, a store whose signatures older signature rules computed is brought to the current
rules (see `_upgrade_signatures`).

Before step 3, every candidate or provisional lesson that is not `human`, not pinned and no longer passes
the render rules (text mined before a rule change) is retired with the rule it breaks, so the inbox never
holds it and a pair that sights it again creates a fresh candidate rather than a twin. The miner never
changes a promoted lesson: one that stops rendering stays as a person approved it, and `scars ls` and
`scars doctor` name it.
6. Expire lessons not sighted for 14 days, then write delayed hit outcomes.

Every transcript-derived text is secret-scanned before it is stored anywhere: a command, error
signature or model id that trips a secret rule is stored as NULL, a working directory as the empty
string (the column is not nullable), a path that does is left out, and a pending failure whose input or
signature does is not kept (it could only pair into a candidate the scan rejects).

A working directory is known only when it is absolute and still exists. An unknown one (empty, relative,
or a deleted worktree) never pairs and names no repo; it is never resolved through a parent directory,
which could belong to another repo. Directories outside registered repos compare by real path, so a
symlinked path and its target are one place.

Event times are the fixing call's time, so expiry counts from the real sighting. Mining the same
transcripts again changes nothing: offsets, processed ids and observations each guard the work.
"""

import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from scars import hook, policy, render, store
from scars.match import InvalidTrigger, Precision, RecordedCall
from scars.mine import build, calls, claude, outcomes, pairs
from scars.mine.replay import CallIndex
from scars.model import (
    ErrorSignature,
    EventType,
    Harness,
    RepoId,
    Status,
    Trigger,
    TriggerKind,
    TrustClass,
)
from scars.signature import RULES_VERSION

DAY = 86400.0


@dataclass(frozen=True)
class Source:
    harness: Harness
    root: Path
    pattern: str


def default_sources(user_home: Path) -> tuple[Source, ...]:
    return (Source(Harness.CLAUDE, user_home / ".claude" / "projects", "*/*.jsonl"),)


@dataclass(frozen=True)
class SignatureUpgrade:
    """What one run did to a store whose signatures the rules of `from_version` computed."""

    from_version: int
    pending_dropped: int
    calls_unpaired: int
    retired: int
    to_review: int
    """Promoted or human lessons with an error trigger, kept as they are and named by `scars doctor`."""


@dataclass(frozen=True)
class Summary:
    sessions_read: int = 0
    """Transcript files with new complete lines."""
    calls: int = 0
    failures: int = 0
    pairs: int = 0
    candidates_created: int = 0
    observations: int = 0
    retired_unrenderable: dict[str, int] = field(default_factory=dict)
    """Mined lessons waiting for review that were retired because their text no longer renders, by rule."""
    pre_triggers_dropped: int = 0
    expired: int = 0
    outcomes_written: int = 0
    models_backfilled: int = 0
    malformed_lines: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    """Candidates dropped by a render rule, by rule name. Never the text."""
    skipped: dict[str, int] = field(default_factory=dict)
    """Pairs that yield no lesson for a structural reason, by reason."""
    signature_upgrade: SignatureUpgrade | None = None
    """Set on the run that moved the store to the current signature rules."""


def session_key(source: Source, path: Path) -> tuple[str, str]:
    """The `(harness, session id)` a transcript file belongs to."""
    return str(source.harness), path.stem


@dataclass(frozen=True)
class _Read:
    offsets: tuple[store.Offset, ...]
    streams: dict[tuple[str, str], list[calls.Call]]
    sessions: int
    malformed: int


def _clean(text: str | None) -> str | None:
    return None if text is None or build.secret_rule(text) is not None else text


def _read(
    sources: Iterable[Source],
    saved: dict[str, store.Offset],
    now: float,
) -> _Read:
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
            session_id = path.stem
            parsed = claude.parse(chunk.lines, session_id, now=now)
            resume = chunk.end if parsed.resume_offset is None else parsed.resume_offset
            offsets.append(store.Offset(key, resume, state.inode, state.mtime))
            malformed += chunk.malformed
            if chunk.lines:
                sessions += 1
            if parsed.calls:
                streams.setdefault((str(source.harness), session_id), []).extend(parsed.calls)
    return _Read(tuple(offsets), streams, sessions, malformed)


def _mined_row(call: calls.Call) -> store.MinedCall:
    """The row to store for a call, with every transcript-derived text secret-scanned."""
    sig = (
        pairs.signature(call.output, command=call.command) if call.status is calls.CallStatus.FAILED else None
    )
    return store.MinedCall(
        tool_use_id=call.tool_use_id,
        harness=str(call.harness),
        session_id=call.session_id,
        at=call.at,
        tool=call.tool,
        tool_kind=str(call.tool_kind),
        command=_clean(call.command),
        paths=tuple(p for p in call.paths if _clean(p) is not None),
        status=str(call.status),
        error_signature=_clean(sig),
        cwd=_clean(call.cwd) or "",
        model_id=_clean(call.model_id),
    )


def _recorded(row: store.MinedCall) -> RecordedCall:
    return outcomes.recorded(row)


def _open_from_store(p: store.PendingFailure, cwd: str | None) -> pairs.OpenFailure:
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
        cwd=cwd,
    )


def _pending_row(f: pairs.OpenFailure) -> store.PendingFailure | None:
    """The row to keep for an open failure, or None when its input or signature holds a secret. Such a
    failure could only pair into a candidate the secret scan rejects, so dropping it loses no lesson."""
    texts = (f.input, f.signature, f.cwd, *([f.program] if f.program is not None else []))
    if any(_clean(t) is None for t in texts):
        return None
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


def known_dir(cwd: str) -> str | None:
    """`cwd` normalized when it is an absolute path to a directory that exists, else None."""
    if not cwd.startswith("/"):
        return None
    path = os.path.normpath(cwd)
    return path if os.path.isdir(path) else None


class _Repos:
    """Working directory to registered repo, owner class and worktree root, resolved once per directory."""

    def __init__(
        self,
        db: sqlite3.Connection,
        common_dir_of: Callable[[str], str | None],
        repo_root_of: Callable[[str], str | None],
    ) -> None:
        self._db = db
        self._common_dir_of = common_dir_of
        self._repo_root_of = repo_root_of
        self._cache: dict[str, tuple[RepoId | None, str | None]] = {}
        self._roots: dict[str, str | None] = {}

    def resolve(self, cwd: str) -> tuple[RepoId | None, str | None]:
        if cwd not in self._cache:
            found = known_dir(cwd)
            common = self._common_dir_of(found) if found else None
            repo = store.repo_for_common_dir(self._db, (common, os.path.realpath(common))) if common else None
            self._cache[cwd] = (repo, store.repo_owner_class(self._db, repo) if repo else None)
        return self._cache[cwd]

    def place(self, cwd: str) -> tuple[str, str] | None:
        found = known_dir(cwd)
        if found is None:
            return None
        repo, _ = self.resolve(cwd)
        return ("repo", repo) if repo else ("dir", os.path.realpath(found))

    def root(self, cwd: str) -> str | None:
        if cwd not in self._roots:
            found = known_dir(cwd)
            self._roots[cwd] = self._repo_root_of(found) if found else None
        return self._roots[cwd]


def _precision(index: CallIndex, trigger: Trigger | None, signature: ErrorSignature) -> Precision | None:
    if trigger is None:
        return None
    try:
        return index.precision(trigger, signature)
    except (InvalidTrigger, ValueError):
        return None


def _write(
    db: sqlite3.Connection,
    candidate: build.Candidate,
    pair: pairs.Pair,
    index: CallIndex,
    counts: Counter[str],
) -> None:
    """Write one pair in one transaction; `counts` changes only when it commits. Skips a pair that
    already has its observation."""
    local: Counter[str] = Counter()
    with store.transaction(db):
        if not store.has_observation(db, pair.failed.tool_use_id):
            _write_pair(db, candidate, pair, index, local)
    counts.update(local)


def _write_pair(
    db: sqlite3.Connection,
    candidate: build.Candidate,
    pair: pairs.Pair,
    index: CallIndex,
    counts: Counter[str],
) -> None:
    at = pair.passing.at
    existing = store.get_lesson_by_fingerprint(db, candidate.fingerprint)
    if existing is not None:
        lesson_id, revision = existing.id, existing.revision
        if not store.observed_in_session(db, lesson_id, pair.failed.harness, pair.failed.session_id):
            last = store.lesson_last_seen(db, lesson_id) or at
            revision = store.apply_event(
                db,
                lesson_id=lesson_id,
                event=EventType.OBSERVED,
                expected_revision=revision,
                actor=policy.ACTOR,
                payload={"fix_program": candidate.fix_program},
                at=max(at, last),
            )
        pre = next((t for t in existing.triggers if t.kind.value in ("command", "path")), None)
        checked = _precision(index, pre, candidate.signature)
    else:
        pre = candidate.pre_trigger
        checked = _precision(index, pre, candidate.signature)
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


def _retire_unrenderable(db: sqlite3.Connection, now: float) -> dict[str, int]:
    """Retire each mined lesson waiting for review whose text no longer renders; counts by rule."""
    pending = store.list_lessons(db, statuses=(Status.CANDIDATE, Status.PROVISIONAL))
    mined = [le for le in pending if le.trust_class is not TrustClass.HUMAN and not le.pinned]
    known = store.passing_inputs(db, [le.id for le in mined])
    repos = store.repo_names(db)
    retired: Counter[str] = Counter()
    for lesson in mined:
        problem = render.render_problem(lesson, known_commands=known[lesson.id], registered_repos=repos)
        if problem is None:
            continue
        try:
            with store.transaction(db):
                store.apply_event(
                    db,
                    lesson_id=lesson.id,
                    event=EventType.RETIRED,
                    expected_revision=lesson.revision,
                    actor=policy.ACTOR,
                    payload={"reason": f"cannot render: {problem.rule}"},
                    at=now,
                )
        except (store.Conflict, store.Rejected):
            continue
        retired[problem.rule] += 1
    return dict(sorted(retired.items()))


def _upgrade_signatures(db: sqlite3.Connection, now: float) -> SignatureUpgrade | None:
    """Bring a store whose signatures older rules computed to `RULES_VERSION` in one transaction, or None
    when it already follows them.

    The store keeps no tool output, so no stored signature can be recomputed and no old error trigger
    confirmed. Pending failures are dropped, stored failed calls lose their signature, and every mined
    candidate or provisional lesson with an error trigger is retired, so a pair sighted again under the
    new rules mines a fresh one. A promoted or human lesson is a person's decision: it is kept and marked
    for review.
    """
    found = store.signature_rules_version(db)
    if found >= RULES_VERSION:
        return None
    with store.transaction(db):
        dropped = store.drop_pending_failures(db)
        unpaired = store.unpair_failed_calls(db)
        waiting = store.list_lessons(db, statuses=(Status.CANDIDATE, Status.PROVISIONAL, Status.PROMOTED))
        keyed = [le for le in waiting if any(t.kind is TriggerKind.ERROR for t in le.triggers)]
        kept = [le for le in keyed if le.status is Status.PROMOTED or le.trust_class is TrustClass.HUMAN]
        for lesson in keyed:
            if lesson not in kept:
                store.apply_event(
                    db,
                    lesson_id=lesson.id,
                    event=EventType.RETIRED,
                    expected_revision=lesson.revision,
                    actor=policy.ACTOR,
                    payload={"reason": "signature rules changed"},
                    at=now,
                )
        store.mark_for_review(db, [(le.id, le.revision) for le in kept])
        store.set_signature_rules_version(db, RULES_VERSION)
    return SignatureUpgrade(found, dropped, unpaired, len(keyed) - len(kept), len(kept))


def run(
    home: Path,
    sources: Iterable[Source],
    now: float,
    *,
    common_dir_of: Callable[[str], str | None] = hook.git_common_dir,
    repo_root_of: Callable[[str], str | None] = hook.repo_root,
    skipped_sessions: frozenset[tuple[str, str]] = frozenset(),
) -> Summary:
    """Mine one batch into `home`/lessons.db and write outcomes into `home`/telemetry.db if it exists.

    `common_dir_of` maps a directory to its git common dir (which names the registered repo), and
    `repo_root_of` to its worktree root (against which path triggers resolve absolute paths).
    `skipped_sessions` names the `(harness, session id)` keys whose transcripts were left out on
    purpose (`scars mine --since`): their pending failures are kept as they are, neither advanced nor
    expired, because the transcript was skipped rather than found idle. Every other session follows
    the normal rules, even one that was read and produced no new calls.
    """
    db = store.init_lessons(home / "lessons.db")
    try:
        store.ensure_miner_schema(db)
        repos = _Repos(db, common_dir_of, repo_root_of)
        return _run(db, home, sources, now, repos, skipped_sessions)
    finally:
        db.close()


def _run(
    db: sqlite3.Connection,
    home: Path,
    sources: Iterable[Source],
    now: float,
    repos: _Repos,
    skipped_sessions: frozenset[tuple[str, str]] = frozenset(),
) -> Summary:
    counts: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    upgrade = _upgrade_signatures(db, now)
    retired = _retire_unrenderable(db, now)
    read = _read(sources, store.miner_offsets(db), now)
    all_ids = [c.tool_use_id for stream in read.streams.values() for c in stream]
    seen = set(store.seen_tool_uses(db, all_ids))
    streams: dict[tuple[str, str], list[calls.Call]] = {}
    for key, stream in read.streams.items():
        for call in stream:
            if call.tool_use_id not in seen:
                seen.add(call.tool_use_id)
                streams.setdefault(key, []).append(call)
    new_calls = [c for stream in streams.values() for c in stream]
    since = now - policy.PRECISION_WINDOW_DAYS * DAY
    stored = store.mined_calls_since(db, since)
    # A pending failure's own call is a stored mined call; its row holds the working directory.
    cwds = {r.tool_use_id: r.cwd for r in stored}
    waiting: dict[tuple[str, str], list[pairs.OpenFailure]] = {}
    for p in store.pending_failures(db):
        failure = _open_from_store(p, cwds.get(p.tool_use_id))
        waiting.setdefault((p.harness, p.session_id), []).append(failure)
    found: list[pairs.Pair] = []
    still_open: list[pairs.OpenFailure] = []
    for key in sorted(set(streams) | set(waiting)):
        if key in skipped_sessions:
            still_open.extend(waiting.get(key, ()))
            continue
        result = pairs.pair_session(waiting.get(key, ()), streams.get(key, ()), now, repos.place)
        found.extend(result.pairs)
        still_open.extend(result.still_open)
        counts["failures"] += result.failures
        if result.operand_mismatch:
            skipped["operand_mismatch"] += result.operand_mismatch
        if result.too_many_fields:
            skipped["structured_too_many_fields"] += result.too_many_fields
    rows = [_mined_row(c) for c in new_calls]
    recent = [_recorded(r) for r in stored]
    recent += [_recorded(r) for r in rows if r.at >= since]
    index = CallIndex(recent, repos.root)
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
            _write(db, candidate, pair, index, counts)
        except (store.Conflict, store.Rejected):
            skipped["store_conflict"] += 1
    store.commit_batch(
        db,
        offsets=read.offsets,
        seen=[c.tool_use_id for c in new_calls],
        pending=[r for r in map(_pending_row, still_open) if r is not None],
        calls=[r for r in rows if r.at >= since],
        prune_before=since,
    )
    expired = policy.expire(db, now)
    result = outcomes.run(db, str(home / "telemetry.db"), now, root_of=repos.root)
    return Summary(
        sessions_read=read.sessions,
        calls=len(new_calls),
        failures=counts["failures"],
        pairs=len(found),
        candidates_created=counts["candidates_created"],
        observations=counts["observations"],
        retired_unrenderable=retired,
        pre_triggers_dropped=counts["pre_triggers_dropped"],
        expired=expired,
        outcomes_written=result.outcomes,
        models_backfilled=result.models,
        malformed_lines=read.malformed,
        rejected=dict(sorted(rejected.items())),
        skipped=dict(sorted(skipped.items())),
        signature_upgrade=upgrade,
    )


__all__ = ["SignatureUpgrade", "Source", "Summary", "default_sources", "run"]
