# ADR-0004: Trust tiers decide where a lesson may appear

Status: accepted, 2026-10-29, revised after review round 2. Answers C06, C13, R03, R06 and the panel split recorded in CRITIQUE "Agreement map".

## Context

The README made human promotion the only way a lesson left its own session. The DX and research critics showed that this leaves the store empty. The security critic showed that sharing anything unreviewed opens a poisoning channel.

The first version of this ADR shared a provisional lesson only after a second session had seen the same failure and fix. The round-2 measurement (`research/experiments/mine_pairs_v3.py`) shows what that costs. In the last 30 days, Claude Code sessions produced 23 repeats of no-edit failure-then-fix pairs. Sharing after the second sighting plus the 2-hour mining interval would have reached 6 of them. Sharing after the first sighting would have reached 20. Over all history the numbers are 19 against 55 out of 58.

## Decision

A lesson's status, stage and source trust class decide where it may appear.

| Status | How it gets there | Repair stage | Plan stage | Gate (deny) | Scope reach |
| --- | --- | --- | --- | --- | --- |
| candidate | Mined pair that fails the provisional rules, or `scars add` in Phase 2 | the writing session only | no | no | its own session |
| provisional | Automatic, from one no-edit failure-then-fix pair: trust class `own_code`, repo scope, and it passes the sanitizer, the imperative lint and the secret scan. Its error trigger always ships. Its command or path pre-trigger ships only if it passes the precision check | yes, labeled "unreviewed" | no | no | its repo only |
| promoted | Human, at a terminal (ADR-0003) | yes | only if also pinned | Phase 2 only, at level `block` after the false-positive replay passes | repo or machine, as the human sets |
| retired or superseded | Human action, mute, or expiry | no | no | no | none |

- Provisional lessons expire after 14 days unless promoted. Each new sighting resets the clock.
- Trust classes are `own_code`, `third_party_repo` (errors seen while working in an upstream repo), `web`, and `human`. Lessons with evidence from `third_party_repo` or `web` stay candidates until a human promotes them, and they can never reach level `block`.
- Promotion sets three things separately: reach (repo or machine), plan-stage pin (yes or no), and level (`note`, `warn`, or in Phase 2 `block`). The inbox defaults to the lesson's current scope, no pin, and `note`.
- Editing a lesson's text, triggers or scope returns it to `candidate`, clears its pin and level, and discards its precision-check and verification results. It must be promoted again. A machine-scope lesson cannot be a candidate, so an edit keeps it promoted but clears its pin, sets its level to `note`, and discards those results (R06).
- Machine-scope lessons always need a human, because they reach every repo.

## Consequences

- Most automatic value arrives without review: 20 of 23 repeats in the last 30 days were reachable.
- Unreviewed text appears only after a failure, inside one of the user's own repos, as quoted data, and for at most 14 days after its last sighting.
- Review is needed only for reach beyond one repo, for plan-stage pins, and for denies.
