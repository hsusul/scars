# ADR-0006: Safety guards stay independent of the lessons store

Status: accepted, 2026-10-29, revised after review round 2. Answers C08, C09 and R10.

## Context

`~/.claude/hooks/guard-bash.sh` already denies full `xcodebuild` runs and `git stash`, and asks before `git push` and `gh pr`. The README proposed folding it into the store as a promoted block lesson. That would make a safety control depend on the store's health, on the matcher, and on a hook that fails open. A corrupt row or a bad retirement would silently disable it.

## Decision

- Safety-critical guards remain standalone hooks that do not read the store. They may fail closed.
- The scars hook fails open. Any internal error returns exit code 0 and empty stdout.
- The scars hook never emits `allow`, `ask` or `updatedInput`. An `allow` would skip a permission prompt that `guard-bash.sh` or the user's settings would otherwise show.
- `scars import-guard` reads a guard's deny reasons and creates promoted, pinned plan-stage lessons only, never gate lessons. The plan stage teaches the rule before the attempt, while the guard keeps enforcing it.
- The first version of this ADR said the adapter would suppress a scars deny when the guard already denied the same call. That cannot work, because both harnesses run matching PreToolUse hooks concurrently and do not pass one hook's output to another (R10). Duplicate denies are avoided statically instead. `scars level <id> block` refuses when the lesson's trigger overlaps a pattern found in an installed standalone guard script.

## Consequences

- The 11 sessions that hit the `xcodebuild` guard show the guard working. What scars adds is a plan-stage note that prevents the attempt, not a second enforcement layer.
- Two mechanisms exist side by side on purpose. One is for safety and the other is for teaching.
