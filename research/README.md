# Shared lessons log for parallel agent sessions

Research workspace, 2026-10-29. Market research, engineering research, gaps, and an MVP spec.

## Superseded, read the docs first

This README is the original research. An eight-critic expert panel reviewed it, and probes settled the disputed facts. The current spec lives in `docs/`:

- [PRD](../docs/PRD.md) is the product spec, with phases, gates and kill criteria.
- [Architecture](../docs/ARCHITECTURE.md) is the system design, with schema, hook contract, matching and miner.
- [Threat model](../docs/THREAT-MODEL.md) and [eval plan](../docs/EVAL-PLAN.md).
- [Critique](../docs/CRITIQUE.md) lists every panel finding and where it was resolved. The `docs/adr/` directory holds six decision records.

Corrections to this README, from the critique:

- A PreToolUse hint does not prevent the attempt. Claude Code delivers PreToolUse `additionalContext` alongside the tool result, after execution. Only a deny acts before execution. The recommendation below that "a scars shown before the attempt would" stop it is wrong (C01, ADR-0001).
- The repeat counts below (1,480 errors, 214 repeats, 52 avoidable) are inflated. Resumed sessions copy earlier errors, and the signature normalization had bugs. Without the pair rule, the corrected upper bound is 92 repeats over all history (`experiments/repeats-v2.json`). Under the miner's actual rule, sessions from the last 30 days produced 23 failure-then-fix repeats, and scars could have reached 20 of them (`experiments/pairs-v3-30d.json`). The PRD now predicts the project stops at its cheap Phase 0 (C05, R04).
- The per-writer JSONL store is replaced by two SQLite files (ADR-0002). The 64-writer race runs quoted below were printed but not saved. Only the 16-writer JSON files are in `experiments/`.
- The ETH AGENTS.md study found that specific instructions are followed and broad repository overviews hurt. Agent KB's gate is a similarity filter at inference time. Letta's LoCoMo agent used embedding search. The text below states all three incorrectly (C15).
- Claude Code's PostToolUse fires only on success. Failures arrive on PostToolUseFailure (C02).

Method. 14 Claude Haiku 5.5 scouts each covered one slice in parallel (memory vendors, native harness memory, parallel orchestrators, memory MCP servers, academic work, storage concurrency, hook APIs, trust and poisoning, market sizing, demand signals, OSS projects, evals, transcript mining, and this workspace). Two experiments ran on this machine (`experiments/`). The two claims the recommendation rests on were re-fetched from primary docs by hand.

Evidence labels used below. **Measured** means run here, with the script in `experiments/`. **Fetched** means a scout or I opened the primary page. **Snippet** means it was seen only in search results. **Inferred** or **guess** means no direct source.

No earlier notes on this idea were found in chat memory or in this workspace. This doc starts from the one-line concept.

## Recommendation

Build it, but not as a shared notes file. Build **scarss**. A scars is a lesson with a trigger (a command pattern, a path glob, or an error signature). A hook delivers it at the moment an agent is about to repeat the mistake, in any harness, and a verification gate stops one bad session from teaching the whole fleet.

Three findings drive this.

1. **A plain shared notes file already exists for Claude Code users.** Claude Code auto memory is "per repository, shared across worktrees" and loads the first 200 lines or 25KB at session start (fetched, [memory docs](https://code.claude.com/docs/en/memory)). Cross-session messaging (v2.1.224+) passes findings between live sessions, including across worktrees (fetched, [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)). GitHub Copilot Memory is repo-scoped, shared by the cloud agent, CLI and code review, and on by default for Pro since 2026-03-04 (snippet, [changelog](https://github.blog/changelog/2026-03-04-copilot-memory-now-on-by-default-for-pro-and-pro-users-in-public-preview/)).
2. **Passive notes do not change behavior reliably.** claude-code [#37314](https://github.com/anthropics/claude-code/issues/37314) updated memory 5+ times for one issue and the error kept coming back. [#51735](https://github.com/anthropics/claude-code/issues/51735) logs 19 repeat incidents in a month. Both were closed as not planned (fetched). The ETH Zurich AGENTS.md study found that LLM-generated context files changed success by -0.5% on SWE-bench Lite and -2% on AGENTbench, and raised inference cost by more than 20% (fetched abstract, [arXiv 2602.11988](https://arxiv.org/abs/2602.11988)).
3. **Your own transcripts show the repeats.** 483 local Claude Code sessions produced 1,480 tool errors (measured, `experiments/mine_repeats.py`). 124 error signatures recurred in 2+ sessions of the same project, giving 214 repeat occurrences after the first. That is an upper bound, because some signatures are generic. The top 20 signatures alone hold at least 52 clearly avoidable repeats. One example is "Full xcodebuild froze the Mac before", which a PreToolUse hook blocked in 11 separate sessions. The guard prevented the damage, but each session still spent a turn trying.

Point 3 is the design argument in one line. A block hook stops the action but does not stop the attempt. A scars shown before the attempt would.

Business judgment. As a venture-backed standalone company this is risky. Every major harness now ships its own repo memory, and each one is locked to its own harness. As an open-source CLI plus hooks for power users who run many sessions across several harnesses, it is worth building. For your own workflow (52 worktrees of `openwhispr` alone, about 15 upstream repos in flight per the notes) it pays off on your own data.

## Market map

| Category | Players (selected) | Shares across parallel sessions? | Gap for this idea |
| --- | --- | --- | --- |
| Harness-native memory | Claude Code auto memory and CLAUDE.md, Copilot Memory, Codex AGENTS.md and Memories, Gemini CLI Auto Memory, Kiro steering, Windsurf memories, Cline Memory Bank | Within one harness only. Loads at session start. | Locked to one harness. Delivery is at session start, not action time. No verification. |
| Live session messaging | Claude Code cross-session messaging, Claude Code agent teams (mailboxes, shared task list) | Yes, live, but ephemeral | Nothing persists for future sessions. The receiver must be running. |
| Memory platforms | Mem0 (about 68k stars, $24M seed plus Series A), Letta (shared memory as git repos), Zep/Graphiti (temporal graph), Supermemory ($3M, coding-agent team memory, $19 and $100 tiers), Cognee, LangMem, MemOS | Per user or org namespace | General-purpose. No worktree, session, or trigger model. Concurrency semantics undocumented. |
| Memory MCP servers | Official server-memory, basic-memory (about 4.4k stars), mcp-memory-service, OpenMemory, Graphiti MCP, Serena memories, ByteRover/Cipher | Through a shared server | Official server corrupts under concurrent writes ([#2578](https://github.com/modelcontextprotocol/servers/issues/2578)). Models rarely use MCP resources. |
| OSS self-improvement plugins | claude-mem (star counts conflict, 46k to 105k), compound-engineering (about 25.8k), claude-reflect (about 1.9k), letta-code MemFS, Beads (Yegge) | Partly. Each writes one shared store with no parallel-writer design. | Session-centric or end-of-task capture. None has triggers, verification, or cross-harness delivery. |
| Parallel orchestrators | Conductor, Claude Squad (8.7k), Vibe Kanban (28.5k, sunsetting), container-use, Sculptor, Superset, Cursor cloud agents | No. Conductor docs say to copy context between workspaces by hand. | They isolate state well but have no channel for learnings. Possible distribution partners. |
| Team-knowledge SaaS | Unblocked ($29/seat, $20M A), CodeRabbit learnings, Greptile memory, Qodo rules from PR history, Stack Internal MCP | Org-wide, for review or Q&A | Learns from review feedback or existing docs, not from agent failures. Enterprise sales. |
| Trace mining | LangSmith Insights ($1 to $4 per 1k threads), Docent, Braintrust Patterns, Langfuse error analysis | Offline clustering | Built for production LLM apps, not local coding-agent transcripts. |

Market size. No credible source counts developers who run several agents in parallel, so the core user base is unsized. Stack Overflow's 2026 survey reports 26% of developers using agents, while another 2026 source says 65% (snippet, [survey](https://survey.stackoverflow.co/2026/ai/data)). Coding-agent spend runs about $150 to $250 per developer per month (snippet, [DX](https://getdx.com/blog/ai-coding-assistant-pricing/)). A $20 to $30 add-on is small against that spend. No source sizes the memory sub-segment.

## Demand evidence

- claude-code [#24798](https://github.com/anthropics/claude-code/issues/24798) is from a user running 5 parallel sessions: "I copy-paste between sessions. Defeats the purpose of parallel agents." Cross-session messaging now covers the live half of this request. The persistent half is still open.
- claude-code [#38536](https://github.com/anthropics/claude-code/issues/38536) asks for team memory shared across engineers and agents (fetched).
- codex [#19758](https://github.com/openai/codex/issues/19758) is open. Codex memories are read-only to the agent ("Never update memories"), and the issue asks for agents to save "repeated mistakes, and validated approaches" (fetched).
- OpenAI community threads from June to October 2026 ask why Codex repeats the same mistakes (snippet).
- Mitchell Hashimoto's harness-engineering loop is to turn every agent mistake into tooling or AGENTS.md (snippet, secondary quote). Every's compound engineering frames it as each unit of work making the next easier (snippet).
- The demand scout rated problem demand as moderate to strong and demand for this exact product as moderate. Upvote counts were mostly not captured.

## Gaps worth building into

Ranked by evidence strength and by how little the platforms cover them.

1. **Action-time delivery.** Every native system loads memory at session start. A PreToolUse hook that matches the pending command or path against lesson triggers delivers the lesson when it applies. This also handles sessions that are already running, with no push channel. Session A writes a lesson, and session B reads it at its next matching tool call. MCP resource push cannot do this portably, because Codex does not subscribe ([codex #16159](https://github.com/openai/codex/issues/16159), snippet).
2. **Cross-harness.** You run Claude Code, Codex and Remy against the same repos. Claude Code, Codex, Cursor, Gemini CLI, Cline and Copilot all have hook systems with SessionStart and PreToolUse-style events (fetched for the first four). One store with thin per-harness adapters covers them all.
3. **Cross-repo machine lessons.** Auto memory is per repo. Your most common env lesson is per machine. The volume path `/Volumes/990 Pro` contains a space, which breaks tox `{toxinidir}` and the jemalloc build in uv (local scout, `phoenix-PR_DRAFTS.md`, `uv-2026-10-15-pr-draft.md`).
4. **Verification and lifecycle.** No product verifies a lesson. The academic results that hold up use environment feedback (Voyager's executable skills) or a disagreement gate (Agent KB: GAIA pass@3 55.2% to 73.9%, SWE-bench pass@1 24.3% to 28.3%, fetched [arXiv 2507.06229](https://arxiv.org/abs/2507.06229)). A lesson that carries a reproduce command can be re-checked when the commit changes.
5. **Mining instead of self-reporting.** Agents rarely write lessons unprompted. Claude Code and Codex transcripts are local JSONL with tool results linked to tool calls (fetched, third-party format write-up). `experiments/mine_repeats.py` scans 483 sessions in 3.5 seconds (measured). Gemini CLI Auto Memory already mines transcripts into a human-review inbox (fetched, [docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/auto-memory.md)), which validates the pattern.
6. **Poisoning blast radius.** The OWASP 2026 agentic top 10 lists memory and context poisoning as ASI06 (fetched). AgentPoison reports over 80% attack success at under 0.1% poison rate (fetched, [arXiv 2407.12784](https://arxiv.org/abs/2407.12784)). Microsoft's catalog names the fleet case, where one compromised agent seeds shared memory (fetched). Per-session files make rollback one `rm`. A promotion gate keeps unverified lessons out of block mode.
7. **Incremental curation.** ACE shows that wholesale rewrites of a playbook lose detail ("context collapse"), and that delta updates gain 10.6% on agent tasks (fetched, [arXiv 2510.04618](https://arxiv.org/abs/2510.04618)). An append-only event log is that delta model.

## Engineering design

### Data shape

The store is an append-only event log. Current lesson state is a reduction over events. This gives idempotent writes, a full audit trail, and rollback by writer.

```ts
type LessonId = string & { readonly __brand: "LessonId" }   // sha256(scope + trigger + normalized claim), first 16 hex chars
type WriterId = string & { readonly __brand: "WriterId" }   // `${harness}-${sessionId}`

type Scope =
  | { kind: "machine" }                                      // env quirks: paths with spaces, sim UDIDs
  | { kind: "repo"; repo: string }                           // git remote URL, normalized
  | { kind: "path"; repo: string; glob: string }

type Trigger =
  | { kind: "command"; pattern: string }                     // regex on the Bash command
  | { kind: "path"; glob: string }                           // Edit/Write/Read target
  | { kind: "error"; signature: string }                     // normalized first informative error line
  | { kind: "session-start" }

type Evidence =
  | { kind: "repro"; run: string; expect: "fail" | "pass" }  // re-runnable check
  | { kind: "transcript"; writer: WriterId; turn: string }
  | { kind: "human" }

type LessonEvent =
  | { type: "proposed"; id: LessonId; claim: string; scope: Scope; trigger: Trigger; evidence: Evidence; at: string; by: WriterId }
  | { type: "verified"; id: LessonId; commit: string; at: string; by: WriterId }
  | { type: "promoted"; id: LessonId; enforcement: "hint" | "warn" | "block"; at: string; by: "human" }
  | { type: "superseded"; id: LessonId; by_lesson: LessonId; at: string }
  | { type: "retired"; id: LessonId; reason: string; at: string }
  | { type: "hit"; id: LessonId; at: string; by: WriterId; outcome: "shown" | "blocked" | "ignored-then-failed" }

type Status = "candidate" | "verified" | "promoted" | "retired"   // derived, never stored
```

The lifecycle is a state machine. A lesson moves from candidate to verified when its repro check passes in a clean worktree, and from verified to promoted when a human approves it. Any state can go to retired. Only promoted lessons can block. Candidates show only in the session that wrote them and are marked as unverified there. `hit` events give usage counts for decay and for the eval.

### Storage

```
~/.lessons/<writer-id>.jsonl                        machine scope
$(git rev-parse --git-common-dir)/lessons/<writer-id>.jsonl   repo and path scope, seen by every worktree
```

Each session appends only to its own file, so no two processes share a write target. Readers glob the directory, parse, and reduce. Records are deduplicated by event content, so a retried write is a no-op.

Why per-writer files when shared appends held up in testing:

- **Measured.** 16 and 64 processes appending JSONL records of 256B to 1MB with one `write()` each on `O_APPEND` gave 0 corrupt and 0 missing records in 7 runs across the internal APFS disk and the external `990 Pro` APFS volume (`experiments/append_race.py`, 14 cores). The read-modify-write pattern used by single-file memory servers kept 11 to 45 of 800 to 3,200 writes, which means 95% to 99.6% were lost.
- **Not guaranteed.** POSIX promises no interleaving only for pipes up to PIPE_BUF, not for regular files (fetched, [Open Group write()](https://pubs.opengroup.org/onlinepubs/9699919799/functions/write.html)). The clean results above are likely XNU holding a per-file lock during each write. That is inferred, not checked in the kernel source.
- **Blast radius.** One file per session means removing a poisoned session's lessons is deleting one file.
- **Verified locally.** In `openwhispr-wt-2095`, `git rev-parse --git-common-dir` resolves to `openwhispr/.git`, which is shared by all 52 worktrees (measured).

An index is optional. If lookups get slow, rebuild a SQLite file from the JSONL in WAL mode with `busy_timeout` and `BEGIN IMMEDIATE`, and treat it as disposable. Do not put it on a network or cloud-synced volume (snippet, SQLite WAL docs). For cross-machine or team sync later, push the lessons directory to a dedicated orphan ref such as `refs/lessons/*`, never to user branches.

### Capture

| Path | Trigger | Default status |
| --- | --- | --- |
| Hook: PostToolUse error then success | A failed tool call followed by a passing call on the same tool within N turns | candidate, evidence = transcript |
| Hook: user correction | UserPromptSubmit text that redirects the previous action ("no, don't", "use X instead") | candidate, flagged for review |
| CLI or MCP tool `lessons add` | The agent or a human writes a lesson directly | candidate, or promoted when a human writes it |
| Miner, run at session end or on cron | Error signatures seen in 2+ sessions (`mine_repeats.py` logic) | candidate, with the session count attached |

### Delivery

| Hook | What it injects | Budget |
| --- | --- | --- |
| SessionStart | Promoted lessons for machine and repo scope, highest hit count first | about 1.5KB, guess, to be tuned by the eval |
| PreToolUse | Lessons whose command or path trigger matches the pending call | at most 3, deny the call only for promoted lessons set to block |
| PostToolUse on error | Lessons whose error signature matches | at most 2 |

PreToolUse reads the directory on every call. That is how a lesson written by session A reaches session B mid-run with no push channel. The cost is hook latency. I guess under 20ms for a few hundred lessons, which the MVP must measure.

Harness adapters. Claude Code hooks have SessionStart, PreToolUse, PostToolUse and an "add context for Claude" output (fetched). The scout's copy of the page was clipped, so the exact field names and size limits still need checking. Codex hooks have the same events plus an `additionalContextLimit` cap (fetched). Cursor hooks have sessionStart and preToolUse, but cloud agents skip sessionStart (fetched). Gemini CLI has BeforeTool and SessionStart, but the page says Antigravity CLI replaced it on 2026-06-18 (fetched). Every adapter is a thin shell over one `lessons match --json` command.

### Integration points already on this machine

- `tools/contrib` already does claims (`new <slug>`), overlap checks (`overlap <path>`) and push-time rechecks. `new` should print the repo's promoted lessons, and `push` should check the stale-base lesson (local scout).
- The `reflect` skill routes accepted learnings into skills with user approval, and `recall` rebuilds context from transcripts. The miner's candidates feed `reflect`, and `recall` reads the store.
- An existing PreToolUse hook already blocks full `xcodebuild`. That hook is a promoted block lesson written by hand. The tool generalizes it.

## Seed lessons from this workspace

Found by the local scout in your notes and by the miner in your transcripts. These make a realistic first eval set.

- Machine. The volume path contains a space. Run tox commands by hand in the venv, and set `CARGO_TARGET_DIR` outside the repo for uv and jemalloc builds.
- Machine. Use `swiftc -parse` instead of a full `xcodebuild` (11 sessions hit the block).
- Machine. A shell cwd inside a removed worktree fails ("Working directory ... was deleted"). This appears in 17 sessions across three signatures.
- Machine. `sleep N; tail` is blocked. Use the Monitor tool with an until-loop (9 sessions).
- Machine. Simulator calls with a stale UDID fail (6 sessions). Omit the device or list devices first.
- Repo. Corti and AssemblyAI both show 1,736 local `npm test` failures, identical on pristine main (ESM interop). browser-use shows 6 on clean main. Record the baseline failure set per repo.
- Repo. Policy gates. Astral forbids autonomous PRs and wants the body in the author's words. Letta needs an AI-disclosure checklist. Phoenix needs an issue first, a `fixes #N` first line and a CLA comment. React Email needs an issue first and PRs that target `canary`.
- Repo. aws-sdk-pandas #3431 picked up a commit from sibling PR #3430 and had to revert. Check the branch base against sibling branches before pushing.
- Workflow. Every fix note repeats the same red/green proof. `remy-scratch/red_green_commit.sh` encodes it but hardcodes one venv path. It belongs in a skill, not in a lesson.

## MVP spec

Scope for about one week of work.

1. `lessons` CLI in Python, stdlib only. The commands are `add`, `list`, `match --tool --input`, `verify <id>`, `promote <id>`, `retire <id>`, and `mine`.
2. Event log and reducer as specified above. A property test checks that 64 concurrent writers plus a reducer converge, reusing `append_race.py`.
3. Claude Code and Codex hook adapters for SessionStart, PreToolUse and PostToolUse.
4. A miner that turns `mine_repeats.py` into candidate lessons, so a human promotes instead of writing.
5. `lessons review`, a TUI or plain prompt to promote or retire candidates, similar to Gemini's `/memory inbox`.

Non-goals for the MVP. No embeddings. Triggers are regex and glob, and the Letta LoCoMo result shows file plus grep tools score 74% without a vector store (fetched, [Letta blog](https://www.letta.com/blog/benchmarking-ai-agent-memory/)). No server, no team sync, no web UI, no LLM summarization in the write path.

## Eval plan

The question is whether scarss cut repeat mistakes without adding the cost the AGENTS.md study found.

- **Offline replay, cheapest first.** Build the store from sessions before a cutoff date. For each later session, count error signatures that a promoted lesson would have matched before the failing call. This gives an upper bound on prevented repeats with zero agent runs.
- **Live A/B.** Use the same task list and repo in two arms, store off and store on. Run each task at least 5 times with the model, tools, resources and timeouts pinned. Report the infra-error rate separately, because Anthropic found infra noise moves agentic scores (snippet, [Anthropic](https://www.anthropic.com/research/infrastructure-noise)).
- **Metrics.** Repeat-mistake rate per 100 sessions, by signature. Tokens and wall-clock to the first passing verifier. Lesson precision, meaning the share of injected lessons a judge marks relevant. Injected tokens per session. Stale-lesson harm, measured by retiring or editing lessons mid-run in the style of SWE-Bench-CL's forgetting metric (fetched, [arXiv 2507.00014](https://arxiv.org/abs/2507.00014)).
- **Decision rule, set before running.** Ship if the repeat rate drops by at least 30% with an injected-token cost under 5% and no drop in task success. Treat effects smaller than the infra noise as null.

## Risks and open questions

- **Platform absorption.** Anthropic, GitHub and Google can add triggers to their own memory. The defense is being cross-harness and local-first. If you only ever use Claude Code, auto memory plus a few hand-written hooks may be enough. That would be the honest result of the offline replay.
- **Noise.** LLM-written context hurt in the ETH study. That is why only promoted lessons go fleet-wide and SessionStart has a budget.
- **Hook drift.** Cline is moving hooks into SDK plugins, Gemini CLI is being replaced, and Cursor CLI does not fire every hook event (snippets). Keep adapters thin.
- **Secrets.** Mined error text can contain tokens or paths. Redact before writing, as claude-mem does with `<private>` tags and regex rules (fetched).
- **Instruction-like text in tool output.** 10 of the 14 scouts reported text in tool results that told them to re-plan or spawn agents. I did not trace whether that came from web pages or from the harness. Either way, a miner that ingests tool output will see such text. That is one more reason candidates never block or go fleet-wide without promotion.
- **Open.** Is the right machine scope `~/.lessons`, or should it live under each harness's config dir? Should promotion require a passing repro, or is human approval enough for policy lessons such as maintainer rules?

## Scout coverage and confidence

All 14 slices returned. The local scout failed once because its shell tool forced a workspace copy that a symlink broke, and it was rerun with read-only tools. The strongest evidence is the Claude Code docs, the Gemini auto-memory doc, the hook docs for four harnesses, the core papers (ACE, Agent KB, AgentPoison, MINJA, ETH AGENTS.md, SWE-Bench-CL), OWASP, and both local experiments. The weakest evidence is pricing and funding, which come from snippets and may be stale, and star counts for claude-mem and ECC, which conflict across sources. Copilot Memory and Codex memory mechanics are snippet-only. Not covered: Amp, Factory and Devin memory internals (no primary source found), Hacker News threads, and r/cursor.

## Sources

Links that scouts saw only as search snippets are marked (s). `experiments/check_links.py` checked all 61 links in this file, and every one returned HTTP 200 on 2026-10-29. A 200 shows that the page exists, not that it says what the summary claims.

Harness memory and hooks. [Claude Code memory](https://code.claude.com/docs/en/memory). [Claude Code cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging). [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams). [Claude Code hooks](https://code.claude.com/docs/en/hooks). [Codex hooks](https://learn.chatgpt.com/docs/hooks). [Cursor hooks](https://cursor.com/docs/hooks). [Gemini CLI hooks](https://geminicli.com/docs/hooks/). [Gemini CLI auto memory](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/auto-memory.md). [Kiro steering](https://kiro.dev/docs/steering/). [Copilot Memory changelog](https://github.blog/changelog/2026-03-04-copilot-memory-now-on-by-default-for-pro-and-pro-users-in-public-preview/) (s). [Copilot agentic memory blog](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/) (s). [Cline Memory Bank](https://docs.cline.bot/best-practices/memory-bank) (s). [MCP resources spec](https://modelcontextprotocol.io/specification/2025-06-18/server/resources).

Vendors and OSS. [Letta shared memory](https://docs.letta.com/concepts/shared-memory/). [Mem0](https://github.com/mem0ai/mem0). [Supermemory for coding agents](https://supermemory.ai/coding-agents/). [Zep pricing](https://www.getzep.com/pricing/) (s). [basic-memory](https://github.com/basicmachines-co/basic-memory). [Official MCP memory server](https://github.com/modelcontextprotocol/servers/tree/main/src/memory). [claude-mem](https://github.com/thedotmack/claude-mem). [claude-reflect](https://github.com/BayramAnnakov/claude-reflect). [compound-engineering-plugin](https://github.com/everyinc/compound-engineering-plugin). [letta-code](https://github.com/letta-ai/letta-code). [Conductor parallel agents](https://www.conductor.build/docs/concepts/parallel-agents). [Claude Squad](https://github.com/smtg-ai/claude-squad). [Vibe Kanban](https://github.com/BloopAI/vibe-kanban). [Unblocked pricing](https://getunblocked.com/pricing/) (s). [CodeRabbit learnings](https://docs.coderabbit.ai/knowledge-base/learnings) (s). [Greptile memory](https://www.greptile.com/docs/how-greptile-works/memory-and-learning) (s). [Mem0 funding](https://www.prnewswire.com/news-releases/mem0-raises-24m-series-a-to-build-memory-layer-for-ai-agents-302597157.html) (s).

Issues and demand. [claude-code #24798](https://github.com/anthropics/claude-code/issues/24798). [claude-code #37314](https://github.com/anthropics/claude-code/issues/37314). [claude-code #38536](https://github.com/anthropics/claude-code/issues/38536). [claude-code #51735](https://github.com/anthropics/claude-code/issues/51735). [codex #19758](https://github.com/openai/codex/issues/19758). [codex #16159](https://github.com/openai/codex/issues/16159) (s). [MCP servers #2578](https://github.com/modelcontextprotocol/servers/issues/2578). [Stack Overflow 2026 AI survey](https://survey.stackoverflow.co/2026/ai/data) (s). [DX on AI coding spend](https://getdx.com/blog/ai-coding-assistant-pricing/) (s).

Research. [ACE](https://arxiv.org/abs/2510.04618). [ReasoningBank](https://arxiv.org/abs/2509.25140). [Agent KB](https://arxiv.org/abs/2507.06229). [Reflexion](https://arxiv.org/abs/2303.11366) (s). [Voyager](https://arxiv.org/abs/2305.16291) (s). [Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988). [SWE-Bench-CL](https://arxiv.org/abs/2507.00014). [MemoryAgentBench](https://arxiv.org/abs/2507.05257). [Letta on LoCoMo](https://www.letta.com/blog/benchmarking-ai-agent-memory/). [Anthropic on infra noise](https://www.anthropic.com/research/infrastructure-noise) (s). [MINJA](https://arxiv.org/abs/2503.03704). [AgentPoison](https://arxiv.org/abs/2407.12784). [Memory poisoning defense](https://arxiv.org/abs/2601.05504). [OWASP agentic top 10 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/). [Microsoft memory poisoning](https://learn.microsoft.com/en-us/security/zero-trust/catalog-ai-attack-techniques/ai-memory-context-poisoning).

Engineering. [POSIX write()](https://pubs.opengroup.org/onlinepubs/9699919799/functions/write.html). [SQLite WAL](https://www.sqlite.org/wal.html). [git rev-parse](https://git-scm.com/docs/git-rev-parse). [Docent analysis plans](https://docs.transluce.org/analysis/analysis-plans). [LangSmith Insights](https://docs.langchain.com/langsmith/insights). [claude-code-log](https://github.com/daaain/claude-code-log). [Codex session size issue #24948](https://github.com/openai/codex/issues/24948).

## Files

- `experiments/append_race.py` runs the concurrent-writer race test. The results are in `race-internal.json` and `race-external.json`.
- `experiments/mine_repeats.py` is the read-only transcript miner. The results are in `repeats-claude-projects.json`. It writes aggregate counts and the first 100 characters of each top signature, and it may contain local paths.
- `experiments/check_links.py` checks every link in this README.
