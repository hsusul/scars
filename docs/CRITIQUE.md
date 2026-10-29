# Expert panel critique of the lessons-log plan

This document records how eight expert critics reviewed the plan in `../research/README.md`, what they found, and how each finding was resolved. Every finding has an ID (`C01` and up). The PRD, the architecture doc, the threat model, the eval plan, and the ADRs cite these IDs where they answer them. `../scripts/check_traceability.py` fails if an "act on" or "consider" finding is not cited by at least one of those docs.

## Panel

Each critic read the README and the experiments, worked read-only with web access, and returned structured findings. The panel spread across five model families so that agreement between critics means more than one model agreeing with itself.

| Critic | Model | Verdict |
| --- | --- | --- |
| Staff distributed-systems and storage engineer | GPT-6.1 Sol | build with changes |
| Principal security engineer and threat modeler | Claude Opus 5.5 | build with changes |
| Agent-memory and LLM researcher | Gemini 4 Argon | build with changes |
| Evaluation and measurement scientist | Kimi K3 | build with changes |
| Harness-integration engineer and SRE | Claude Opus 5.5 | build with changes |
| Developer-experience and CLI product engineer | Claude Sonnet 5.5 | build with changes |
| Product lead and market strategist | GPT-6.1 Terra | build with changes |
| Pre-mortem skeptic | Grok 4.7 | do not build |

Two critics (storage and product) ended with a model error after they submitted their findings. Their findings passed schema validation and are used in full.

The skeptic's "do not build" is not dismissed. It became the Phase 0 gate in the PRD. Phase 0 builds the cheap alternative the skeptic proposed and measures it before any store is built (C11).

## Probes run during synthesis

Where critics disagreed on a fact, the disagreement was settled by measurement or by the primary source, not by argument.

- **PreToolUse context timing.** The harness critic and the skeptic disagreed about whether Claude Code shows PreToolUse context to the model. The Claude Code hooks reference settles it. `hookSpecificOutput.additionalContext` on PreToolUse is "added to Claude's context alongside the tool result", so the model reads it after the tool ran. Only `permissionDecision: "deny"` acts before execution. Verified from the docs for the installed Claude Code 2.1.299. A live probe (`../research/experiments/probe_pretooluse_context.sh`) could not run because the Claude Code login on this machine had expired. That probe is INCONCLUSIVE and is listed as an open item.
- **Harness usage.** In the last 30 days this machine produced 474 Claude Code transcript files and 300 Codex rollout files, counted by modification time (`../research/experiments/evidence/census.txt`). Two harnesses are in real use.
- **Repeat counts, corrected.** `../research/experiments/mine_repeats_v2.py` removes 84 errors (6% of 1,385) that resumed sessions copied from earlier transcripts. It also fixes the path, traceback and UUID normalization bugs and adds timestamps. With "Exit code" lines, timeouts and hook denials excluded, 54 signatures recurred, giving 92 repeat occurrences across 484 Claude Code sessions. Other signature variants give 118 to 143. Only 5 of the 92 repeats happened while the first session was still running. A lesson available when the first session ended would have reached 87 of them, and one available a day later would have reached 66. Result file `../research/experiments/repeats-v2.json`.
- **Store read latency.** A fresh Python process reading a 1,000-lesson SQLite store runs at p50 23 ms and p99 24 to 38 ms, idle and with 16 concurrent writers, on both disks (`../research/experiments/sqlite_latency.py`, 100 samples per row). Reading per-session JSONL files took about 80 ms at 2,000 files and 170 to 196 ms at 5,000 (`../research/experiments/hook_latency.py`). Python startup sets the 20 ms floor. The probe does not include shell parsing or the telemetry write (R17).
- **Pair repeats, added in review round 2.** `../research/experiments/mine_pairs_v3.py` applies the miner's real rule (a failure followed by a fix with no edit in between). For the 248 sessions that started in the last 30 days it finds 23 pair repeats. Sharing after the first sighting would have reached 20 of them, and sharing after a second sighting 6 (`../research/experiments/pairs-v3-30d.json`). These replace the 92 as the number the product acts on (R04).

## Findings

Buckets follow the interrogate lead-judgment rules. **Act on** means the plan was wrong or would fail without the change. **Consider** means a real point that the docs adopt in part or schedule for later. **Noted** means valid but not actionable now. **Dismissed** means rejected, with the reason.

### Act on

| ID | Finding | Raised by | Resolution |
| --- | --- | --- | --- |
| C01 | A PreToolUse hint cannot prevent the attempt. The model has already chosen the call, and non-deny context arrives with the tool result, after execution. The README's central claim was wrong. | harness, research, skeptic, product | Delivery is split into three stages: plan (SessionStart, UserPromptSubmit, PostCompact), gate (PreToolUse deny only), and repair (failure events). Non-blocking PreToolUse hints are dropped. ADR-0001, ARCHITECTURE "Delivery stages". |
| C02 | Claude Code's PostToolUse fires only on success. Failures arrive on PostToolUseFailure. Codex has no failure event, and its SessionEnd is capped at 3 s, too short for a miner. | harness, research | A per-harness event map. The miner never runs inside a session hook. ARCHITECTURE "Harness event map". |
| C03 | No trust boundary. `by: "human"` is a string any session can write, writer identity is self-declared, and "rollback is one rm" is false. | security, storage | Revised in round 2 (R01). The boundary is the harness sandbox, with privileged commands limited to an interactive terminal and denied to agents. A hash-chained event log makes tampering detectable. Rollback is an audited state change, never a file delete. ADR-0003, THREAT-MODEL T1 to T3. |
| C04 | `lessons verify` would run an agent-written shell string outside any sandbox. | security | Repro evidence is an argv array with an allowlisted executable. It runs only after a human confirms, under `sandbox-exec` with no network. Phase 1 ships without automatic verify. ARCHITECTURE "Verification", THREAT-MODEL T4. |
| C05 | The headline numbers (214 repeats, 52 avoidable) were inflated. They included copied history, and normalization bugs created junk signatures and merged unrelated errors. There were no timestamps, so "avoidable" was untestable. | eval, research, product, skeptic | Re-measured (see Probes). The PRD uses 92 lessonable repeats with a 92 to 143 range and the timing split. The 50-sample human labeling in EVAL-PLAN turns the upper bound into an estimate. |
| C06 | Nobody owns the review queue, so the promoted set stays empty. Promotion bundled trust, reach and enforcement into one decision. | DX, research | Trust tiers. Provisional lessons share automatically as repo-scoped repair hints. Human review is needed only for plan-stage pinning and for block. The inbox is capped, ranked and one keystroke per item. ADR-0004, PRD "Review journey". |
| C07 | The per-writer JSONL plus reducer design had no ordering or conflict rules, no stable identity, no crash handling, and read cost that grew with history. | storage, harness, eval | A single SQLite store holds events and materialized state in one transaction, with revisions, compare-and-set, and command IDs. Measured read p99 is 38 ms or less. ADR-0002, ARCHITECTURE "Store". |
| C08 | Failure behavior differs by harness. Claude Code's default hook timeout is 600 s, and Cursor blocks the tool call on invalid JSON. A broken store must never block an agent or fail silently. | harness, security | The hook catches every error, exits 0 with empty stdout, and sets a 2 s timeout. A kill switch, self-telemetry and `scars doctor` cover the rest. ARCHITECTURE "Hook contract". |
| C09 | The existing `guard-bash.sh` safety guard must not move into the store. A scars must never emit `allow`, `ask` or `updatedInput`. | harness, security | Safety guards stay as independent hooks. scars output is limited to context and deny. ADR-0006. |
| C10 | The eval was underpowered by 4 to 20 times, its thresholds were unjustified, the token metric was undefined, hit events lacked the fields the analysis needs, and there was no baseline arm. | eval, product, skeptic | Instrumented baseline week, a trap-task design, absolute thresholds with intervals, behavioral proxies instead of an LLM judge, and a hand-written-hooks baseline arm. EVAL-PLAN. |
| C11 | The plan never weighed the simplest alternative (hand-written hooks, a rules file, and a weekly miner report), and the one-week MVP bundled too many unproven bets. | skeptic, product | Phase 0 builds that alternative first and is the baseline. Later phases need measured gates. PRD "Phases and gates". |
| C12 | Free regex triggers on raw commands miss shell wrappers and compound commands, fire on benign calls, and allow ReDoS. Matching ran without a deadline. | research, security, DX | Commands are parsed into simple commands with wrappers unwrapped. Trigger grammar is limited to tokens and anchored globs, with an optional safe regex subset. Input is capped at 4 KB, matching has a deadline, and `scars test` must replay a trigger against 30 days of history and keep its false-positive rate under budget before it ships. ARCHITECTURE "Matching". |
| C13 | Lesson text is a prompt-injection channel into every session, and harness output from third-party repos feeds the miner. | security | Structured fields only, a sanitizer and imperative lint, rendering as quoted data with provenance, and a trust class per source. Third-party output can never back a block lesson. THREAT-MODEL T5 and T6. |
| C14 | The miner's redaction misses tokens in URLs and common key formats. Machine-scope lessons can carry private names into public PRs. | security | Secret detection at write time and again at render time, with candidates refused rather than partially redacted. Machine scope gets a lint, and rendered lessons get an "internal" tag. THREAT-MODEL T7. |
| C15 | The README misread three sources. The ETH AGENTS.md study found that specific instructions are followed and repository overviews hurt. Agent KB's gate is an inference-time similarity filter, not a repro check. Letta's LoCoMo agent used embedding search. | research | PRD "Evidence" restates all three correctly. The README carries a corrections section. |

### Consider

| ID | Finding | Raised by | Resolution |
| --- | --- | --- | --- |
| C16 | Lessons phrased as declarative facts get ignored. Use a contrastive avoid/instead/reason form and re-inject after compaction. | research | Adopted. The lesson record is `avoid`, `instead` and `reason`, and PostCompact re-injects pinned lessons. ARCHITECTURE "Data model". The format comparison probe is in EVAL-PLAN. |
| C17 | The miner should capture the failed input, the error, and the passing input, and only when no source edit happened between them. Otherwise ordinary test-driven debugging floods the store. | research | Adopted for the Phase 1 miner. ARCHITECTURE "Miner". |
| C18 | Ranking by hit count feeds itself. There is no "followed" outcome, and a lesson cannot hold both a pre-trigger and an error signature. | research, eval | Adopted. Hit outcomes are `heeded`, `ignored_then_failed`, `fired_unneeded` and `blocked`, and a lesson may carry both kinds of trigger. Ranking uses utility with decay. ARCHITECTURE "Data model". |
| C19 | Adapters are not thin. Tool inputs differ by harness, Cursor also loads Claude hooks so it can fire twice, and Remy's hook API is unknown. | harness | A normalized event shape with golden fixtures per harness version. Cursor and Remy are out of scope until probed. ARCHITECTURE "Adapters". |
| C20 | Install must go through harness plugin mechanisms. `~/.claude/settings.json` is co-managed by another tool (`managed_by=headroom`), and Codex skips untrusted hooks. The hook binary must be pinned outside agent workspaces. | harness, security | Adopted. ARCHITECTURE "Install", THREAT-MODEL T8. |
| C21 | Scope identity needs a machine ID, a logical repo ID, and a clone registry. Forks and remote aliases are ambiguous, and the git common dir is a discovery path, not a durable home. | storage, security | Adopted. The store lives in `~/.scars`, and the common dir is used only to find which repo a worktree belongs to. ARCHITECTURE "Scope and identity". |
| C22 | Verification must bind to the exact lesson revision and execution context. | storage | Adopted for Phase 2 verification records. ARCHITECTURE "Verification". |
| C23 | There is no five-minute onboarding, no "why did this fire", no mute, no doctor, and no digest. | DX | Adopted. `scars init` mines history and shows would-have-fired counts, plus `scars why`, `scars test`, `scars mute`, `scars doctor` and a weekly digest. PRD "User journeys". |
| C24 | The doc used three names (scarss, `lessons`, `~/.lessons`). "scars" may collide with an existing security product. | DX, product | One working name, `scars` with the CLI `scars`. The collision check is a PRD open question. |
| C25 | The market map was too categorical. Cursor attaches rules when matching files enter context, and Copilot checks memory citations against the branch. | product | PRD "Competitive position" compares by delivery stage and trigger kind instead. |
| C26 | Token cost must count re-billing of injected text on every later call. Cap injection per call and show each lesson once per session per trigger. | eval, harness | Adopted as non-functional requirements. PRD NFR-4. |
| C27 | The append race experiment did not commit its 64-writer runs and did not test crashes, concurrent readers, or fsync. | eval, storage | The SQLite store replaces that design. Crash and kill tests move to the store's fault tests. ARCHITECTURE "Store", and the README notes the uncommitted runs. |
| C28 | Public launch needs independent pilot users, launch criteria, kill criteria, and a distribution plan (Claude Code plugin plus pipx). | product | Adopted as Phase 3 public-release criteria. PRD "Phases and gates". |
| C29 | Native platforms may reach parity within 6 to 12 months. What survives is ownership, audit, export, and cross-harness portability. | product | PRD "Risks". |
| C30 | Use BM25 or semantic matching at UserPromptSubmit for plan-stage recall. | research | Deferred. Phase 1 uses keyword and path matching on the prompt. BM25 is added only if the eval shows a plan-stage recall gap. EVAL-PLAN "Format and recall probes". |

### Noted

- Codex SessionEnd and subagent hooks share the parent session ID (harness). The SQLite store does not depend on writer identity, so this is moot for storage. It matters only for telemetry attribution.
- Touch ID on every promotion may be too much friction (security). Moot after round 2, which removed signing from the single-user design (R01).
- SWE-Bench-CL measures forgetting in a trained model, not stale lessons in a static store (eval). EVAL-PLAN defines stale-lesson harm directly.

### Dismissed

- "Start with Claude Code only" (product). The census shows 300 Codex sessions in 30 days, so both harnesses are in scope from Phase 1. Remy stays out until its hook API is confirmed.
- "PreToolUse `updatedInput` should rewrite commands, for example setting `CARGO_TARGET_DIR`" (research). A silent rewrite hides the lesson from the model and skips the permission prompt (C09). A deny with the corrected command teaches the same thing visibly.
- "Make the whole store signed JSONL for audit" (security keep-list). Signing applies only to privileged events. The SQLite event table is the audit log.

## Agreement map

The strongest signal is C01. Four critics on four model families (Claude, Gemini, Grok, GPT) independently found that a PreToolUse hint cannot prevent the attempt, and the primary docs confirm it. C05 (inflated numbers) was raised by four critics on four families, and the re-measurement confirms it. C07 (storage protocol) was raised by GPT and Claude critics, who disagreed on the fix (SQLite versus a compiled snapshot). The latency probe settled that, because one SQLite file serves as both.

The panel split on one question. Should anything be shared without human review? The research and DX critics want automatic provisional sharing so that review is not a bottleneck. The security critic wants nothing unreviewed to leave its own session. ADR-0004 resolves it. Provisional lessons share only within their repo scope, only as repair-stage hints after a failure has already happened, only from own-code trust class, after sanitization, the lints and the secret scan, and they expire 14 days after their last sighting. They never reach plan-stage injection or block. Round 2 measured the cost of waiting for a second sighting before sharing: it would have reached 6 of 23 repeats in the last 30 days, against 20 of 23 for the first sighting, so provisional lessons share after the first sighting.

The skeptic is the only "do not build". The other critics all named real gaps that hand-written hooks do not cover: machine-scope lessons, repair hints across sessions, and mining. The round-2 measurement made the skeptic's case stronger. Under the miner's real rule, Claude Code produced 23 pair repeats in the last 30 days, and the automatic path would have reached 20. The most common ones are machine-wide habits that Phase 0 handles with hand-written rules. The PRD now predicts that gate G0 fails and the project stops at Phase 0, unless Codex mining changes the count.

## Review round 2

After the docs were written, three reviewers on three other model families read them with one shared prompt: GPT-6.1 Sol (not ready, 10 findings), Grok 4.7 (not ready, 5 findings), and Gemini 4 Argon (ready with fixes, 10 findings). A trail auditor on GPT-6.1 Terra checked `../research/decisions.tsv` against its evidence (8 flags). The GPT reviewer and the trail auditor ended with a model error after submitting. Their results passed schema validation and are used in full. Findings were merged by substance. Each row names the reviewers who raised it.

### Act on

| ID | Finding | Raised by | Resolution |
| --- | --- | --- | --- |
| R01 | The HMAC design cannot work. Verifying an HMAC needs the key it protects, so the hook would prompt on every read. Signing events also left the materialized lesson rows unauthenticated. | GPT | Signing removed. The boundary is the harness sandbox, plus terminal-only privileged commands and hash-chained tamper detection. ADR-0003, THREAT-MODEL T1. |
| R02 | The hook opened the store read-only but had to write telemetry and dedupe rows. | GPT, Grok, Gemini | Two files: `lessons.db`, read-only in hooks, and `telemetry.db`, best-effort writes after output. ADR-0002, ARCHITECTURE "Hook contract". |
| R03 | Phase 1 both required and excluded signing and provisional sharing. | GPT, Grok, Gemini | One Phase 1 scope across PRD, ARCHITECTURE and ADR-0004: provisional repair hints and terminal-only pins are in Phase 1, while `block`, verify and quarantine are in Phase 2. |
| R04 | The 92 repeats were not what the miner keeps, had no time window, and "about 30 occurrences" counted sessions. The timing figures were upper bounds stated as reach. | GPT, Grok, Gemini, trail | Re-measured with `../research/experiments/mine_pairs_v3.py`: 23 pair repeats in 30 days, of which 20 were reachable. The PRD evidence and ADR-0005 now label the old figures as upper bounds. |
| R05 | The Codex gate matcher named `exec_command`, which Codex reports as `Bash`, so the gate would never fire. Codex compaction uses `SessionStart` with the `compact` source. `model_id` is missing on some hook events. | Gemini | Matcher `Bash\|apply_patch`, compaction via `SessionStart`, and a nullable `model_id` backfilled by the miner. ARCHITECTURE "Adapters", ADR-0001. |
| R06 | Editing a promoted lesson kept its pin and level. | GPT | An edit returns the lesson to candidate and clears its pin, level and precision result. ADR-0004, ARCHITECTURE "Lifecycle". |
| R07 | Once-per-session dedupe would let a denied command run on retry, and blocked re-injection after compaction. | GPT | Denies are never deduplicated. Advisory dedupe is keyed by context epoch, which goes up on each compaction. ARCHITECTURE "Delivery stages". |
| R08 | Delayed outcomes such as `heeded` cannot be known at hook time, and `hits` lacked the fields the eval needs. | GPT, Gemini, Grok | The hook writes only immediate outcomes. The miner's outcome pass computes delayed ones. `hits` gained `tool_use_id`, `lesson_revision`, `status_at_injection` and `trigger_key`. ARCHITECTURE "Miner" step 11, EVAL-PLAN "Definitions". |
| R09 | Fingerprints built from command text would never merge across sessions. The utility formula, the human-notice channel, and two tables were undefined. | Gemini, Grok | Fingerprint on scope plus error signature. The utility and inbox formulas are written out. Human notices use `systemMessage`. `cwd_repo` and `precision_runs` tables added. ARCHITECTURE "Data model", "Adapters". |
| R11 | Pair extraction and its evidence storage were underspecified. | GPT | The pair rule is spelled out in steps, and an `observations` table and `seen_tool_use` dedupe across batches were added. ARCHITECTURE "Miner". |
| R12 | The shell parser was undefined, its whitespace fallback could cause wrong matches, and the "safe regex subset" could not be enforced. | GPT | A bounded parser with listed wrappers, unsupported forms skipped with no fallback, and no regular expressions in Phase 1. ARCHITECTURE "Matching". |
| R13 | PRD and EVAL-PLAN gates disagreed, and the success non-inferiority check was underpowered. | GPT | EVAL-PLAN "Thresholds" is the single source. The success check is stated as a guard against clear harm, with INCONCLUSIVE as an explicit result. |

### Consider

| ID | Finding | Raised by | Resolution |
| --- | --- | --- | --- |
| R10 | Suppressing a duplicate deny at runtime is impossible, because PreToolUse hooks run concurrently. | Gemini | Replaced by a static check when a lesson is set to `block`. ADR-0006. |
| R14 | Phase 1 was over-engineered for the measured benefit. | Gemini | Command IDs, verification, quarantine, install hashing and `chflags`, export and import, and nine CLI commands moved to Phase 2. Compare-and-set and the event log stay, because each is a few lines. PRD "CLI surface", ARCHITECTURE "Install and distribution". |
| R15 | CLI and event names disagreed across docs. | Gemini, Grok | One CLI list per phase in the PRD, and `level_set` used everywhere. |
| R16 | Several cited fixes were not specified: the precision rule for shared triggers, the machine-scope lint, and the Adapters section. | Grok | Each is now a written rule. ARCHITECTURE "Matching", "Rendering" and "Adapters". |
| R17 | The latency probe left out shell parsing and the telemetry write, and the 1,670 transactions per second figure was not in the result file. | Gemini, trail | The budget table marks the end-to-end number as not measured. `scars bench` is a Phase 1 acceptance check (PRD FR-10). ADR-0002 states only the row count. |
| R18 | Evidence pointers were unsaved commands. | trail | Saved: `../research/experiments/evidence/census.txt`, `../research/experiments/evidence/claude-hooks-excerpt.md`, `../research/experiments/logs/probe_pretooluse_context.log`, and the traceability output in `../research/experiments/logs/`. |

## Confirmation round

Final status: after the fixes below, a fresh GPT-6.1 Sol reviewer re-checked the last three open contradictions (the telemetry lock timeout, provisional eligibility, and the outcome window and trap endpoint). It returned "ready" with no other blockers. Grok 4.7 and Gemini 4 Argon had returned "ready with fixes" before those fixes.

Fresh reviewers on the same three families checked each R01 to R18 resolution against the revised docs. Grok 4.7 returned "ready with fixes" (16 resolved, 2 partial, no new blocker). Gemini 4 Argon returned "ready with fixes" (16 resolved, 2 partial, 1 new blocker). GPT-6.1 Sol returned "not ready" (10 resolved, 8 partial). The partial items and new problems were merged into the findings below and fixed in the docs.

### Act on

| ID | Finding | Raised by | Resolution |
| --- | --- | --- | --- |
| R19 | The hook opened `telemetry.db` only after printing, but it needs the repo cache, the context epoch and the dedupe rows before matching. Writes waited up to 50 ms on a lock outside the latency budget. | Gemini, GPT | Telemetry is read before matching and written after output, both with `busy_timeout = 0`. The latency budget covers the process from start to exit. ARCHITECTURE "Hook contract", PRD NFR-1. |
| R20 | `scars add`, supersede and edit were in Phase 1 in ARCHITECTURE but Phase 2 in the PRD. | Gemini, GPT, Grok | The lifecycle table has a phase per event. Editing in Phase 1 goes through the inbox `e` key. ARCHITECTURE "Lifecycle", ADR-0004. |
| R21 | The privileged-command lists disagreed across ADR-0003, the hook contract and the PRD. | Grok, GPT | One list in ARCHITECTURE "Hook contract", cited by ADR-0003 and the PRD. |
| R22 | UserPromptSubmit admitted unpinned promoted lessons, which ADR-0004 forbids. | GPT | All plan-stage injection requires a pin. ARCHITECTURE "Delivery stages". |
| R23 | The precision check gated provisional status, although provisional lessons fire only on error triggers. | Gemini | The check gates only pre-triggers. ADR-0004, ARCHITECTURE "Matching". |
| R24 | Outcome rules overlapped, had no reference call for plan-stage hits, and wrote `unknown` for sessions still running. | Gemini, GPT | An ordered rule list, a reference call per stage, a `no_attempt` outcome, and outcomes held back until the session ends or idles 24 hours. ARCHITECTURE "Miner" step 11, EVAL-PLAN "Definitions". |
| R25 | THREAT-MODEL T9 still described regex matching, which Phase 1 removed. | Grok | T9 rewritten. THREAT-MODEL T9. |
| R26 | The trap-task endpoint was undefined when a lesson prevented the failure. The match-path latency gate was missing from the thresholds. When the proxy failed, G1 could pass on trap tasks alone. | GPT, Grok | The endpoint is turns from task start to the first successful target operation. The match-path row was added. A failed proxy is replaced by the user's labels, not skipped. EVAL-PLAN "Step 3", "Thresholds". |
| R27 | An edit could not return a machine-scope lesson to candidate. Fingerprints ignored path and session scope. Pending failures across batches had no storage. Reach was timed from the failure instead of the fix. | GPT | Machine-scope edits stay promoted with pin and level cleared. Path and session were added to the fingerprint, and a `pending_failures` table was added. Reach was re-measured from the fix time, with unchanged counts. ARCHITECTURE "Lifecycle", "Miner", PRD "Evidence". |
