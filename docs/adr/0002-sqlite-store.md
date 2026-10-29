# ADR-0002: Two SQLite files, one for lessons and one for telemetry

Status: accepted, 2026-10-29, revised after review round 2. Supersedes the per-writer JSONL design in the research README. Answers C07, C21, C27, R02.

## Context

The README proposed one append-only JSONL file per session in the git common dir, reduced on every read. The panel found four problems. It had no rule for concurrent promote and retire, no stable lesson identity, and no crash handling, and its read cost grew with history. The latency probe confirmed the last one. A fresh-process reader took about 80 ms at 2,000 session files and 170 to 196 ms at 5,000 (`research/experiments/hook_latency.py`).

The first version of this ADR put telemetry in the same file as the lessons, while the hook opened that file read-only. All three round-2 reviewers caught that a read-only connection cannot insert telemetry rows or record which lessons a session has already seen (R02).

## Decision

- Lessons live in `~/.scars/lessons.db`. Only the CLI and the miner write it. The hook opens it with `mode=ro` and a 200 ms busy timeout.
- Hook-written state lives in `~/.scars/telemetry.db`: the `hits` rows, the `shown` deduplication rows, the context epochs, and the directory-to-repo cache. The hook reads it before matching and writes it after printing, both with `busy_timeout = 0`. If it is busy, the hook continues without the cache or dedupe and drops its writes (ARCHITECTURE "Hook contract", R19). A telemetry problem never changes or delays the hook's answer.
- Both files use WAL mode, live on the internal disk, and have mode 0600 in a 0700 directory. CLI writers use `busy_timeout=2000`.
- Lesson changes use compare-and-set: `update lessons set ... , revision = revision + 1 where id = ? and revision = ?`. A changed row count of 0 means a conflict, and the CLI reports it. Each change also appends one row to the `events` table in the same transaction. That table is the audit log, and `scars why` reads it.
- Lessons have random stable IDs. A separate fingerprint is used only to merge sightings of the same lesson (ARCHITECTURE "Miner").
- JSONL is only an export and import format. The git common dir is used only to find which repo a worktree belongs to (C21).

## Evidence

`research/experiments/sqlite_latency.py` measured a fresh Python process opening a 1,000-lesson store read-only and running one query plus a regex. Idle, it ran at p50 23.1 ms and p99 38.1 ms on the internal disk. With 16 processes inserting rows in `BEGIN IMMEDIATE` transactions, it ran at p50 23.9 ms and p99 25.9 ms, and those writers inserted 100,192 rows during the run. On the external `990 Pro` volume the p99 was 24.4 to 24.6 ms. That is 100 samples per row. The probe did not include the telemetry write or shell parsing, so it does not yet prove the full hook budget. Phase 1 acceptance includes a full-hook benchmark (ARCHITECTURE "Performance budget").

## Consequences

- Lifecycle conflicts have a defined winner. The first commit wins.
- A telemetry problem cannot break delivery, and a lessons-store problem makes the hook output nothing.
- Cloud sessions cannot read the local store. That is a stated non-goal.
- SQLite over a network or cloud-synced folder is unsupported. `scars doctor` refuses those locations.
- The append-race experiment no longer carries the design. Its 64-writer runs were printed but not saved, so only the 16-writer JSON files back the README's numbers (C27). Fault tests now target these two files (ARCHITECTURE "Fault tests").
