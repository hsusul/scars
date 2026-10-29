# ADR-0005: Capture lessons in batch from transcripts, not live from hooks

Status: accepted, 2026-10-29, revised after review round 2. Answers C02, C17 and R04, and records the change of premise.

## Context

The idea started as a log that parallel sessions share while they run. The README designed live capture in PostToolUse hooks and a live read on every tool call so that a lesson from session A would reach running session B.

Two measurements tested that premise.

- `research/experiments/mine_repeats_v2.py` compared the time of each repeat with the end of the first session that hit the same error signature. Of 92 candidate repeats, 5 happened while the first session was still running. These are timing upper bounds. They assume a lesson existed the moment the first session ended (R04).
- `research/experiments/mine_pairs_v3.py` applies the miner's real rule: a failure followed by a fix with no source edit in between. In the last 30 days it found 23 pair repeats across 248 sessions. Sharing after the first sighting plus a 2-hour mining interval would have reached 20 of them.

So batch capture every 2 hours reaches most repeats, and live sharing would add at most the remainder. Codex also caps `SessionEnd` hooks at 3 s, so a miner cannot run there (C02).

## Decision

- The miner runs outside agent sessions. A launchd agent runs it every 2 hours, and `scars mine` runs it on demand.
- It reads Claude Code transcripts (`~/.claude/projects/*/*.jsonl`) and Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`) incrementally from a saved byte offset per file.
- It deduplicates copied history by `tool_use_id`. 84 of 1,385 errors were copies.
- It keeps a failure only when the next successful call of the same tool, and for shell calls the same program, comes within 10 calls with no `Edit`, `Write`, `MultiEdit`, `NotebookEdit` or `apply_patch` in between. Ordinary red-green debugging is excluded (C17).
- Hooks do not capture. They read the lessons store and write best-effort telemetry.

## Consequences

- No write path in the hot loop, and no LLM call at capture time.
- The few concurrent repeats are left to Claude Code's native cross-session messaging, which passes findings between running sessions.
- The measured automatic benefit is small: about 20 reachable repeats a month in Claude Code. Codex is not mined yet. PRD Phase 0 decides whether that is worth building.
