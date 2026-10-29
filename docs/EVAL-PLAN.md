# scars eval plan

Status: draft, revised after review round 2, 2026-10-29. This plan decides every gate in `PRD.md`, and its "Thresholds" table is the single source for gate numbers (R13). It replaces the eval section of the research README, which the panel found underpowered and undefined (C10).

## What is being estimated

The question is whether scars reduces repeat failures of known failure-then-fix pairs for one operator, compared with Phase 0's hand-written rules, without lowering task success or adding meaningful token cost (C11).

## Definitions

- **Error signature.** The output of `research/experiments/signature.py` (later `scars/signature.py`).
- **Pair.** A failed call followed within 10 calls in the same session by a passing call of the same tool and program, with no file edit in between. This is the rule in `research/experiments/mine_pairs_v3.py` and ARCHITECTURE "Miner".
- **Pair repeat.** A pair whose signature already produced a pair in an earlier session in the same project, after deduplicating copied history by `tool_use_id`.
- **Preventable.** A human labeler marks a pair repeat as fixable by one specific instruction, and marks it as a real error, not normal output from a command that exits non-zero.
- **Opportunity session.** A session in which at least one active lesson's trigger matched. The primary natural-traffic endpoint uses this denominator. The raw per-session rate is secondary (C10).
- **Hook-time outcomes.** Written by the hook: `shown`, `blocked`, `no_match`, `log_only`, `error`.
- **Delayed outcomes.** Computed by the miner's outcome pass from transcripts, never at hook time (R08). The exact rules, their order, and the reference call for each hit are in ARCHITECTURE "Miner" step 11. In short:
  - `ignored_then_failed`: the lesson's error signature occurs within 5 calls after the reference call, or at the reference call itself for a plan-stage hit. The other outcomes are decided only after that window closes.
  - `fired_unneeded`: a plan-stage hit whose matching call succeeded with no failure following. Repair hits cannot have this outcome.
  - `heeded`: after a repair hit, the next call of the same tool kind succeeded or no longer matches the pre-trigger.
  - `no_attempt`: after a plan-stage hit, the session never made a call matching the pre-trigger. This is the outcome a preventing lesson produces, and also the outcome of an irrelevant one, so it is read only together with the labeled audit below.
  - `unknown`: the session ended before any rule applied.
- **False block (Phase 2).** A deny followed by a user override, or by the same command succeeding later in the session after the user disabled the lesson.
- **Token cost.** The difference in total API input tokens per session, read from transcript usage fields. Injected text is billed again on every later call, so injected character counts understate it (C26).

## Step 1: label the opportunity (Phase 0, before any build)

1. Run `research/experiments/mine_pairs_v3.py --days 30` on Claude Code transcripts. Write the Codex reader and run the same rule on `~/.codex/sessions`.
2. Label every pair repeat from the last 30 days. Claude Code alone has 23, so this takes about 10 minutes. For each one, record whether it is a real error, whether one instruction would prevent it, whether a Phase 0 rule already covers it, and roughly how many turns it cost.
3. Report the count of preventable pair repeats not covered by Phase 0 rules, per month, for both harnesses.
4. Gate G0 uses this count.

## Step 2: instrumented baseline (Phase 1, store in log-only mode)

For the first week of Phase 1, the hooks compute matches and write `hits` rows with `outcome = "log_only"` and `arm = "baseline"`, but output nothing. This measures:

- the real opportunity rate per session,
- the base rate of pair repeats on that denominator,
- the per-session input-token distribution,
- the full-hook latency on real traffic.

## Step 3: comparison

One operator cannot produce enough natural sessions to power a plain A/B test. The eval critic estimated about 1,900 sessions per arm to detect a 30% drop at a base rate of 0.15 (C10). So two designs run together, both with the Phase 0 rules active in every arm. That way the comparison measures what scars adds on top of Phase 0.

**Trap tasks.** Build 12 short tasks, each set up to hit one labeled lesson. Examples: run a command from a removed worktree, drive a simulator with a stale UDID, render a PDF without `pdftoppm`, or run `tox` in a checkout on `/Volumes/990 Pro`. Each task has one target operation, such as a passing test run or a rendered file. Each task runs 4 times in each of two arms, Phase 0 only and Phase 0 plus scars, for 96 runs. Model and harness versions are pinned and logged per run. Arm order is randomized within each day.

The primary measure per run is the number of turns from task start to the first successful target operation. Runs that never fail are kept, so a lesson that prevents the failure counts as a win. A run that hits the 30-turn cap or the 15-minute limit scores 30 turns and counts as a task failure. Per task, the 4 runs of each arm are averaged, and the 12 paired differences go into a Wilcoxon signed-rank test (R13, R26).

**Natural traffic.** In real sessions, scars assigns each new session to on or off with probability 0.5 and records the arm in every `hits` row. Analysis runs once, after a fixed 3 weeks, on the opportunity denominator, with model ID as a covariate. There are no interim looks.

## Thresholds

These numbers are set before running.

| Gate | Metric | Pass |
| --- | --- | --- |
| G0 | Preventable pair repeats per month that Phase 0 rules do not cover, both harnesses | 15 or more |
| G1 | Trap tasks: turns from task start to the first successful target operation, with capped runs scored as in Step 3 | lower with scars in at least 9 of 12 tasks, Wilcoxon p below 0.05 |
| G1 | Natural traffic: pair repeats per opportunity session, scars arm against Phase 0 arm | the point estimate is lower, and the 95% interval does not show an increase above 0.05 per session |
| G1 | Task success in trap tasks | no task loses more than 1 of 4 runs compared with the Phase 0 arm. With 4 runs per task this is a guard against clear harm, not a powered non-inferiority test (R13) |
| G1 | Token cost per session, natural traffic | median increase under 3% |
| G1 | `fired_unneeded` share of plan-stage pre-trigger hits | under 30% |
| G1 | Mute rate | under 10% of distinct lessons shown |
| G1 | Human review time | median 3 minutes a week or less, from inbox timing |
| G1 | Full-hook latency from `scars bench`, process start to exit | p99 50 ms or less with no match, and p99 100 ms or less with a match |
| G2 | False blocks | under 2% of deny firings, and no confirmed harmful instruction |

When a metric cannot be computed, for example because there were too few opportunity sessions, its gate is reported as INCONCLUSIVE. Inconclusive is not a pass.

## Validating the delayed-outcome proxies

There is no LLM judge in the primary metrics (C10). The user labels 50 hits, stratified by delayed outcome, as relevant or not. That takes about 10 minutes. If Cohen's kappa between the labels and the proxy is above 0.6, with `heeded` and `ignored_then_failed` counted as relevant and `fired_unneeded` as not relevant, the proxy stands. If it is not, the gate uses the share of hits the user labeled not relevant in place of the `fired_unneeded` share, with the same 30% limit. The proxy failing does not make the gate pass by default.

## Offline replay (screening only)

Replay splits sessions chronologically. Lessons come from the first 70% and are tested on the last 30%. It reports matches on failing calls, matches on passing calls, misses, and injected characters per 100 calls (C10). Replay cannot see whether an agent would heed a lesson, so it never decides a gate. It feeds the precision check in ARCHITECTURE "Matching".

## Format and recall probes

- **Lesson phrasing (C16).** For 3 seed lessons, inject a declarative fact in one variant and the avoid, instead and reason form in the other. Run 10 single-turn trials of each and compare how often the agent follows it.
- **Plan-stage recall (C30).** In natural traffic, count pair repeats where a promoted lesson existed but no plan or repair trigger fired. If that exceeds 20% of repeats, test BM25 matching over the prompt in Phase 2.
- **PreToolUse timing (C01).** Rerun `research/experiments/probe_pretooluse_context.sh` once Claude Code is logged in, and write the same probe for Codex.

## Instrumentation required from day one

Every `hits` row carries `harness`, `harness_version`, `session_id`, `tool_use_id`, `event`, `stage`, `lesson_id`, `lesson_revision`, `status_at_injection`, `trigger_key`, `outcome`, `injected_chars`, `latency_ms`, `error_class` and `arm`. `model_id` is filled by the hook when the harness provides it and backfilled by the miner otherwise. The schema is in ARCHITECTURE "Data model" (R08).
