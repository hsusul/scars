# scars PRD

Status: draft, revised after review round 2, 2026-10-29. Name `scars`, CLI `scars`.

scars stops coding agents from repeating mistakes that another session on the same machine already hit and fixed. It mines past Claude Code and Codex sessions for failures that were followed by a working fix. It turns each one into a short lesson and shows the lesson where it helps: before the agent plans, right after the same failure happens again, and in Phase 2 as a deny before a harmful command.

This PRD replaces the recommendation in `../research/README.md`. The expert panel and two review rounds in `CRITIQUE.md` found several of the README's claims wrong, and the corrections are built in here. The design is in `ARCHITECTURE.md`, the decisions in `adr/`, the threat model in `THREAT-MODEL.md`, and the measurement plan in `EVAL-PLAN.md`.

## Summary of the recommendation

Build Phase 0 now. It is about 2 days of work: hand-written machine lessons and a weekly mining report. Build Phase 1 only if Phase 0's gate passes. The current data predicts it will not. In the last 30 days, the automatic part of scars would have reached about 20 repeat failures in Claude Code sessions. The most common ones are machine-wide habits that Phase 0 covers by hand. Codex sessions have not been mined yet, and they are the one input that could change this prediction.

## Problem

One developer runs many agent sessions at once across separate git worktrees and about 15 repositories, in two harnesses. In the last 30 days this machine produced 474 Claude Code transcript files and 300 Codex rollout files (`research/experiments/evidence/census.txt`). 248 of the Claude Code sessions started inside that window. The rest were older sessions that were resumed. Session counts grow by one or two between runs as new sessions start, and the numbers here match the committed result files. Each session starts without what earlier sessions learned. Lessons in one tool's memory do not reach the other tool. Lessons that apply to the whole machine do not fit any one repository's memory. One example is the space in `/Volumes/990 Pro`, which breaks `tox` and the jemalloc build.

### Evidence

All numbers come from Claude Code transcripts on this machine. Codex is not mined yet. The scripts, result files, census, and docs excerpts behind them are saved under `research/experiments/` (R18).

- **Pair repeats, the number the product acts on.** `research/experiments/mine_pairs_v3.py` applies the miner's real rule: a failed call followed within 10 calls by a passing call of the same tool and program, with no file edit in between. For sessions that started in the last 30 days, 16 such failure signatures recurred, giving 23 repeats after the first sighting. Over all 486 sessions, 37 signatures gave 58 repeats (`research/experiments/pairs-v3-30d.json`, `research/experiments/pairs-v3-all.json`). Reach is measured from the time of the fix, not the failure (R27). Every fix in the data came within minutes of its failure, so the two give the same counts.
- **What the product could reach.** If a lesson is shared as a repair hint after its first sighting plus the 2-hour mining interval, it would have reached 20 of the 23 repeats in the last 30 days. Waiting for a second sighting would have reached 6. ADR-0004 therefore shares after the first sighting.
- **Upper bounds without the pair rule.** Counting every recurring error signature, not only failure-then-fix pairs, gives 92 repeats (range 92 to 143 across signature definitions) over all sessions. That excludes copies carried into resumed sessions, which were 84 of 1,385 errors (`research/experiments/repeats-v2.json`). The README's 214 figure was inflated (C05, R04).
- **Timing.** In the upper-bound set, 5 of 92 repeats happened while the first session was still running. That is a timing bound that assumes a lesson existed when the first session ended, not a measured delivery (R04).
- **What recurs.** In the last 30 days the top pair signatures are a shell working directory inside a removed worktree (10 sessions across three variants), a stale iOS simulator UDID (5 sessions), and a missing `pdftoppm` (2 sessions). Several remaining top entries are not real errors. Commands such as `git remote -v` and `git diff` exit non-zero while printing normal output. The worktree error is also split across three signature variants, which shifts the count between signatures. So 23 and 20 are uncleaned upper bounds until the labeling step in EVAL-PLAN Step 1 separates real, preventable repeats.
- **Scale.** About 20 reachable repeats a month is modest. Each repeat typically costs one to a few turns. That is why every phase has a kill gate (C11).
- **What research says about delivery.** Specific instructions are followed. The ETH Zurich AGENTS.md study found that broad, LLM-written repository overviews lowered success and raised cost by more than 20%, while concrete instructions were followed (C15). Lessons must stay short, specific, and shown only where they apply.
- **Demand elsewhere.** claude-code issues #37314, #51735 and #24798, and codex issue #19758, report repeated mistakes and ask for shared or agent-writable memory. That shows the problem exists beyond this machine. It does not show that anyone wants this product.

## Users

The primary user is a solo operator who runs 5 to 50 agent sessions at once on one Mac, across several repositories, in Claude Code and Codex. In their words, the job is: "When I launch parallel agents, stop them from repeating the environment and repository mistakes my other sessions already worked out, without me babysitting each one."

Teams, shared organization memory, and cloud-only agents are not users of this version.

## Goals

1. Cut repeat failures of known failure-then-fix pairs in the user's own sessions, measured as defined in `EVAL-PLAN.md`.
2. Never make an agent worse. No blocked work from a scars fault, no measurable drop in task success, and a bounded token cost.
3. Cost the human 3 minutes a week or less of review.
4. Keep the user in control. Every lesson is inspectable, explainable and removable, and all data stays local.

## Non-goals

- Live sharing between sessions running at the same time. Claude Code's cross-session messaging already covers live handoffs (ADR-0005).
- Team or cross-machine sync, cloud agents, Cursor, Gemini CLI, and Remy. Remy's hook API is unknown (C19).
- Embeddings, regular expressions, or LLM calls in Phase 1 (R12).
- Replacing safety guards such as `guard-bash.sh` (ADR-0006).
- Rewriting agent commands. The tool never emits `updatedInput` or `allow` (C09).
- Preventing tampering by a process running as the same user. scars limits and detects it (ADR-0003).

## Competitive position

Comparing by delivery stage and trigger kind is more accurate than the README's market map (C25).

| Capability | Native today | scars |
| --- | --- | --- |
| Lessons loaded at session start | Claude Code auto memory (per repo, shared across worktrees), Codex AGENTS.md and memories, Copilot Memory | Pinned lessons only, within a budget |
| Rules attached when matching files are involved | Cursor glob rules, Claude Code path-scoped rules | Path triggers at UserPromptSubmit |
| Deny with a reason before a harmful call | Hand-written PreToolUse hooks | Phase 2, from promoted lessons with a precision gate |
| Hint right after the same failure recurs | None found | Repair stage, keyed on the error signature |
| Lessons mined from transcripts | Gemini CLI Auto Memory (inbox, off by default) | Claude Code and Codex |
| One store across harnesses and repositories, including machine scope | None found | Yes |
| Memory checked against the current branch | Copilot Memory (agent-judged citations) | Phase 2 verification records |

Only three things are unique to scars: repair-stage hints, machine-scope lessons, and one store shared by two harnesses. Phase 0 tests whether native features plus hand-written rules already cover the need.

## Phases and gates

`EVAL-PLAN.md` "Thresholds" is the single source for every gate number. This section names the gates and their order (C10, C11, R13).

**Phase 0, about 2 days. The cheap alternative, and the baseline.**
- Build: run `research/experiments/mine_pairs_v3.py` and a Codex version of it weekly. Hand-label the top signatures with the protocol in EVAL-PLAN Step 1. Write one machine-lessons block in `~/.claude/CLAUDE.md` and one in `~/.codex/AGENTS.md`, covering the top classes: worktree working directory, simulator UDID, the volume path with a space, and missing tools. Add hand-written hooks only where a deny is needed.
- Gate G0 to Phase 1, after 2 weeks: labeled, preventable pair repeats that the Phase 0 rules do not already cover must reach at least 15 a month, counting both harnesses. Prediction from current Claude Code data: G0 fails. The 23 pair repeats a month include the classes Phase 0 covers by hand and several non-errors. If G0 fails, stop and write up the result.

**Phase 1, about 1 week. Core scars.**
- Build: both SQLite files, the miner with the pair rule and outcome pass, provisional repair hints, plan-stage pins, the inbox, the hooks for Claude Code and Codex, `why`, `test`, `mute`, `doctor`, `bench`, the kill switch, telemetry, and plugin install.
- Not in Phase 1: block level, `verify`, quarantine, `chflags` and manifest hashing, export and import, and team features (R14).
- Gate G1 to Phase 2, after 3 weeks: the EVAL-PLAN Phase 1 thresholds.

**Phase 2, about 1 week. Enforcement.**
- Build: block level with the precision rule, the sandbox check required for block, sandboxed verification records, quarantine, install integrity checks, and export and import.
- Gate G2 to keep it: the EVAL-PLAN false-block threshold, and no confirmed harmful instruction.

**Phase 3, optional. Public release.**
- Build: a Claude Code plugin and a Codex plugin, a pipx-installable CLI, docs, and the name check.
- Release criteria: 10 independent pilot users. At week 4, at least 3 of them still use it weekly, and their measured results meet the Phase 1 thresholds. Kill at 6 weeks if not (C28).

## Functional requirements

Phase 1 unless marked otherwise. Each requirement names the findings it answers.

- **FR-1 Mining.** `scars mine` and a launchd job every 2 hours read new Claude Code and Codex transcript bytes. They deduplicate by `tool_use_id`, extract no-edit failure-then-fix pairs, and write candidates and observations (C05, C17, R11, ADR-0005).
- **FR-2 Lesson shape.** A lesson has `avoid`, `instead` and `reason` fields, each 240 characters or fewer. It has an error trigger and an optional command or path pre-trigger, a scope (machine, repo, path or session), a status, a level (`note` or `warn`, plus `block` in Phase 2), and a trust class (C13, C16, C18).
- **FR-3 Delivery.** Lessons appear only at the stages in ADR-0001: plan, repair, and in Phase 2 gate (C01, C02).
- **FR-4 Trust tiers.** Provisional lessons share automatically as repo-scoped repair hints after their first sighting, under ADR-0004. Machine scope, plan-stage pins and level changes need the human at a terminal (C03, C06, R03).
- **FR-5 Inbox.** `scars inbox` shows at most 5 candidates at a time, ranked as in ARCHITECTURE "Data model". Each takes one keystroke: approve, edit, mute, or skip. At session start, scars shows the human a `systemMessage` line when items are waiting. Unreviewed candidates expire 14 days after their last sighting (C06, C23, R09).
- **FR-6 Explainability.** `scars why <lesson-id>` and `scars why last` show the trigger that matched, the input it matched, the observations behind the lesson, and its event history. `scars test <id>` runs the precision check (C12, C23).
- **FR-7 Control.** `scars mute`, `scars retire`, and the kill switch. The kill switch is `scars off`, the file `~/.scars/OFF`, or `SCARS_OFF=1`, and it takes effect on the next hook call (C08).
- **FR-8 Health.** `scars doctor` reports hook health per harness, sandbox coverage, store location, and the event hash-chain check, and it runs a canary (C08, C20, ADR-0003).
- **FR-9 Onboarding.** `scars init` installs both plugins after showing a dry-run diff. It registers repos, mines existing history, shows the top 5 candidates with "would have fired N times in the last 30 days", and offers to import `guard-bash.sh` deny reasons as pinned plan-stage lessons. The whole flow takes under 5 minutes (C23, ADR-0006).
- **FR-10 Benchmark.** `scars bench` runs the full hook, including the telemetry write, under concurrent load and reports p50 and p99 (R17).
- **FR-11 Verification, Phase 2.** Repro checks are argv arrays with an allowlisted executable. They run only after human confirmation, in a sandbox with no network (C04, C22).

## Non-functional requirements

- **NFR-1 Latency.** The full hook, from process start to exit, stays at p99 50 ms or less on the no-match path and p99 100 ms or less with a match. This is measured by `scars bench` with 1,000 lessons and 64 concurrent hook runs on both disks. The read-only probe reached p99 24 to 38 ms without parsing or the telemetry write (ADR-0002, R17).
- **NFR-2 Fail open.** Any internal error produces exit code 0 and empty stdout. Each hook entry sets an explicit 2 s timeout. A scars fault never blocks a tool call (C08).
- **NFR-3 No silent failure.** Every hook run writes a best-effort telemetry row, and `scars doctor` turns those rows into per-harness health (C08).
- **NFR-4 Context budget.** At session start, plan-stage lessons get 600 characters or fewer. UserPromptSubmit and repair each inject at most 2 lessons and 400 characters. A Phase 2 deny reason is 300 characters or fewer. Advisory lessons show at most once per session per trigger per context epoch. Denies are never deduplicated. Token cost counts re-billing of injected text on later calls (C26, R07).
- **NFR-5 Privacy.** All data stays local. Candidates that fail the secret scan are dropped, not partially redacted (C14).
- **NFR-6 Integrity.** Lesson changes use compare-and-set. The event log is hash-chained and checked by `scars doctor` (ADR-0002, ADR-0003).

## User journeys

**First run.** The user runs `scars init`. It shows the plugin hooks it will add for Claude Code and Codex and asks for confirmation. It registers the repos it finds, mines 30 days of history in a few seconds, and lists the top candidates with "would have fired N times in the last 30 days". The user promotes two and pins the volume-path lesson. In the next session, the session-start context includes that one line.

**Daily use.** The user does nothing. A session in an `openwhispr` worktree runs `cd apps/<dir>` from the worktree root, and the command fails. Right after the failure, the repair hint arrives: `[scars L-7c2e note | repo openwhispr | seen in 7 sessions | unreviewed] AVOID cd apps/<dir> from a worktree root. INSTEAD cd "$(git rev-parse --show-toplevel)" first.` The agent fixes the path on its next call instead of exploring.

**Review.** The next morning, the first session shows the human one line: "3 scars lessons waiting: run scars inbox". The inbox takes about a minute.

**A wrong lesson.** An agent quotes a lesson that does not apply. The user runs `scars why last` and sees that the pre-trigger was `npm test`, which is too broad. They run `scars mute L-91aa`. The lesson stops on the next hook call.

**Something breaks.** Agents feel slow. `scars doctor` shows the Codex hook p99 at 400 ms after a disk issue. The user runs `scars off`, and every session stops consulting the store on its next hook call.

## CLI surface

- **Phase 1:** `scars init`, `scars inbox`, `scars promote`, `scars pin`, `scars unpin`, `scars level` (`note` or `warn`), `scars ls`, `scars why`, `scars test`, `scars mute`, `scars retire`, `scars doctor`, `scars bench`, `scars off`, `scars on`, `scars mine`, `scars import-guard`. Plumbing that hooks call: `scars hook <harness> <event>`.
- **Phase 2:** `scars level <id> block`, `scars verify`, `scars quarantine`, `scars add`, `scars supersede`, `scars export`, `scars import`, `scars repo`, `scars digest`.
- **Agents** may call `scars ls` and `scars why`. The single list of privileged commands is in ARCHITECTURE "Hook contract". The adapter denies agents those commands and `scars inbox` (ADR-0003, R15).

## Risks

- **Native parity.** Anthropic, OpenAI or GitHub may ship error-keyed hints within 6 to 12 months. What would survive is local ownership, machine scope, and one store across harnesses. If native features plus Phase 0 cover the need, stop (C29).
- **Small effect.** About 20 reachable repeats a month may not justify the upkeep. Gate G0 measures this before anything is built.
- **Poisoning and injection.** See `THREAT-MODEL.md`.
- **Hook API drift.** Claude Code and Codex change hook semantics between releases. Golden fixtures per harness version and `scars doctor` catch drift (C19).

## Open questions

1. Does PreToolUse context reach the model before execution in either harness? The Claude Code docs say it arrives with the tool result (`research/experiments/evidence/claude-hooks-excerpt.md`). The live probe could not run because Claude Code was logged out (`research/experiments/logs/probe_pretooluse_context.log`). Rerun `research/experiments/probe_pretooluse_context.sh` after logging in, and write the Codex version.
2. What does Codex mining add? This is the input most likely to change the G0 prediction.
3. Do the Claude Code and Codex sandbox settings on this machine deny writes to `~/.scars` (ADR-0003)?
4. Naming is resolved. The project and its CLI are both named `scars`. The name was free on PyPI, npm and Homebrew when checked on 2026-10-29. Trademarks and GitHub name collisions were not checked (C24).
5. Does Remy expose hooks? The `managed_by=headroom` HTTP hooks in `~/.claude/settings.json` may belong to it. Confirm before planning an adapter.
