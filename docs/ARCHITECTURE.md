# scars architecture

Status: draft, revised after review round 2, 2026-10-29. Read with `PRD.md`. Decisions are recorded in `adr/`. Critique findings are cited as `Cxx` and review-round findings as `Rxx`, both defined in `CRITIQUE.md`.

## Overview

scars is a local Python CLI (`scars`) with two SQLite files, a background miner, and a hook entry point that Claude Code and Codex call through their plugin systems. It has no daemon and makes no network calls.

```
  Claude Code / Codex sessions                                human at a terminal
        |  hook events (stdin JSON)                                 |
        v                                                           v
  scars hook <harness> <event> --read-only--> lessons.db <--writes-- scars inbox, promote, pin, level, retire
        |        \                              ^
        |         \--best-effort--> telemetry.db|  candidate and provisional writes
        v                                       |
  agent context or deny                 scars mine (launchd, every 2 h) --reads--> telemetry.db (outcome pass)
                                                ^
                                                |  incremental read
                         ~/.claude/projects/*/*.jsonl, ~/.codex/sessions/**/rollout-*.jsonl
```

There are three runtime paths.

1. **Hot path.** `scars hook` runs on every hook event the plugin registers. It reads `lessons.db` read-only with one indexed query, matches, renders, and writes one best-effort row to `telemetry.db`. Budget: p99 50 ms (PRD NFR-1).
2. **Batch path.** `scars mine` reads new transcript bytes. It extracts failure-then-fix pairs, writes candidate and provisional lessons, and computes delayed hit outcomes (ADR-0004, ADR-0005).
3. **Human path.** Privileged commands run only at a terminal (ADR-0003).

## Components

| Module | Owns | Phase |
| --- | --- | --- |
| `scars/model.py` | Typed records for lessons, triggers and hook events, plus the lifecycle transition table | 1 |
| `scars/store.py` | Schemas, migrations, compare-and-set updates, the event log with its hash chain, and read queries | 1 |
| `scars/hook.py` | `scars hook` entry, kill switch, error containment, and the telemetry write | 1 |
| `scars/adapters/claude.py`, `codex.py` | Parsing harness stdin into a `HookEvent`, and rendering output in the harness's format | 1 |
| `scars/shell.py` | The bounded shell parser | 1 |
| `scars/match.py` | Trigger evaluation under a deadline | 1 |
| `scars/render.py` | Lesson template, sanitizer, lints, secret scan, size caps | 1 |
| `scars/signature.py` | Error-signature normalizer, shared by the hook and the miner (prototype in `research/experiments/signature.py`) | 1 |
| `scars/mine/` | Transcript readers, pair extraction, trust classification, the outcome pass | 1 |
| `scars/policy.py` | Provisional, expiry and precision rules (ADR-0004) | 1 |
| `scars/cli.py` | All `scars` commands | 1 |
| `scars/verify.py` | Sandboxed repro checks | 2 |

The hot path imports only `hook`, `adapters`, `shell`, `match`, `render`, `signature`, `store` and `model`. Keeping the miner and the CLI out of its import graph keeps startup near the 20 ms Python floor (`research/experiments/hook_latency.py`).

## Data model

The lesson is a typed record. The organizing structures are the lifecycle transition table (see "Lifecycle"), the trigger table indexed by kind and stage, and an append-only event log with a hash chain (ADR-0002, ADR-0003).

### `lessons.db` (written by the CLI and the miner, read-only in hooks)

```sql
create table repos (
  id text primary key,
  display_name text not null,
  owner_class text not null check (owner_class in ('own','upstream'))
);
create table clones (
  common_dir text primary key,                 -- absolute path from git rev-parse --git-common-dir
  repo_id text not null references repos(id),
  registered_at real not null
);
create table lessons (
  id text primary key,                         -- 'L-' + 8 random hex, stable across edits
  revision integer not null default 1,         -- compare-and-set key
  status text not null check (status in ('candidate','provisional','promoted','retired','superseded')),
  level text not null default 'note' check (level in ('note','warn','block')),
  pinned integer not null default 0 check (pinned in (0,1)),
  scope_kind text not null check (scope_kind in ('machine','repo','path','session')),
  repo_id text references repos(id),
  path_glob text,
  session_id text,                             -- set only while scope_kind = 'session'
  avoid text not null check (length(avoid) <= 240),
  instead text not null check (length(instead) <= 240),
  reason text not null check (length(reason) <= 240),
  trust_class text not null check (trust_class in ('own_code','third_party_repo','web','human')),
  fingerprint text not null,                   -- see "Miner", step 6
  sessions_seen integer not null default 1,
  fix_programs text not null default '[]',     -- JSON list of distinct programs used in the passing call
  precision_ok integer not null default 0,     -- 1 when the last scars test passed (cleared on edit)
  created_at real not null, updated_at real not null, last_seen_at real not null,
  expires_at real,                             -- provisional and candidate only
  superseded_by text references lessons(id),
  check (level <> 'block' or status = 'promoted'),
  check (pinned = 0 or status = 'promoted'),
  check (scope_kind <> 'machine' or status in ('promoted','retired','superseded'))
);
create index lessons_active on lessons(status, scope_kind, repo_id);
create unique index lessons_fingerprint on lessons(fingerprint) where status in ('candidate','provisional','promoted');

create table triggers (
  lesson_id text not null references lessons(id),
  kind text not null check (kind in ('command','path','error','prompt')),
  spec text not null,                          -- grammar in "Matching"
  stage text not null check (stage in ('plan','gate','repair')),
  primary key (lesson_id, kind, spec, stage)
);
create index triggers_kind on triggers(kind, stage);

create table observations (                    -- one row per mined failure-then-fix pair (R11)
  id integer primary key,
  lesson_id text not null references lessons(id),
  harness text not null, session_id text not null, failed_tool_use_id text not null unique,
  failed_input text not null,                  -- after secret scan, capped at 1 KB
  error_signature text not null,
  passing_input text not null,                 -- capped at 1 KB
  at real not null
);

create table events (                          -- audit log, read by scars why and scars doctor
  seq integer primary key,
  lesson_id text references lessons(id),
  type text not null,                          -- see "Lifecycle"
  actor text not null,                         -- 'human' | 'miner' | 'agent:<harness>:<session>'
  from_revision integer, to_revision integer,
  payload text not null,                       -- canonical JSON of the change
  prev_hash text not null,                     -- sha256 of the previous row's canonical JSON (ADR-0003)
  at real not null
);

create table miner_offsets (path text primary key, offset integer not null, inode integer not null, mtime real not null);
create table seen_tool_use (tool_use_id text primary key);   -- copied-history dedupe across batches
create table pending_failures (                -- failures still waiting for a fix across batch boundaries (R11)
  tool_use_id text primary key, harness text not null, session_id text not null,
  tool text not null, program text, input text not null, error_signature text not null,
  calls_since integer not null default 0, last_call_at real not null
);
create table precision_runs (lesson_id text not null, revision integer not null, matched_failing integer not null,
                             matched_passing integer not null, window_days integer not null, at real not null,
                             primary key (lesson_id, revision));
```

### `telemetry.db` (written best-effort by hooks, read by the miner and `scars doctor`)

```sql
create table hits (
  seq integer primary key, at real not null,
  harness text not null, harness_version text, model_id text,   -- model_id may be null and is backfilled by the miner (R05)
  session_id text not null, tool_use_id text, event text not null, stage text,
  lesson_id text, lesson_revision integer, status_at_injection text, trigger_key text,
  outcome text not null check (outcome in ('shown','blocked','no_match','log_only','error')),
  injected_chars integer, latency_ms real, error_class text, arm text
);
create index hits_session on hits(session_id, at);

create table outcomes (                        -- delayed outcomes, computed by the miner (R08)
  hit_seq integer primary key,
  outcome text not null check (outcome in ('heeded','ignored_then_failed','fired_unneeded','no_attempt','unknown'))
);

create table shown (                           -- advisory deduplication only (R07)
  session_id text not null, context_epoch integer not null, lesson_id text not null, trigger_key text not null,
  primary key (session_id, context_epoch, lesson_id, trigger_key)
);
create table session_epoch (session_id text primary key, context_epoch integer not null default 0);
create table cwd_repo (cwd text primary key, repo_id text, common_dir text, checked_at real not null);
```

Text fields are the contrastive triple `avoid`, `instead`, `reason`, because paired do-this-not-that rules get followed more than declarative facts (C16). A lesson may carry both a pre-trigger (`command` or `path`) and an `error` trigger, which lets the outcome pass attribute "shown, then ignored, then failed" (C18).

**Utility, used for ranking (C18, R09).** Over the last 30 days of outcomes:

```
utility = (heeded + blocked) - 2 * ignored_then_failed - 0.5 * fired_unneeded
score   = (utility + sessions_seen) * 0.5 ** (days_since_last_seen / 30)
```

The inbox ranks candidates by `sessions_seen * consistency * 0.5 ** (days_since_last_seen / 14)`. Here `consistency` is the share of observations whose passing call used the most common fix program.

## Lifecycle

| Event type | From | To | Who | Phase |
| --- | --- | --- | --- | --- |
| `proposed` | none | candidate | miner in Phase 1, `scars add` in Phase 2 (R20) | 1 |
| `observed` | candidate, provisional, promoted | same, `sessions_seen` plus 1, expiry reset | miner | 1 |
| `provisionalized` | candidate | provisional | miner policy (ADR-0004) | 1 |
| `expired` | candidate, provisional | retired | miner policy, after 14 days with no sighting | 1 |
| `promoted` | candidate, provisional | promoted | human at a terminal | 1 |
| `pinned`, `unpinned` | promoted | promoted | human at a terminal | 1 |
| `level_set` | promoted | promoted. `block` needs a `precision_runs` row for this revision that passes the block rule | human at a terminal | `note` and `warn` in 1, `block` in 2 |
| `edited` | any active | candidate, with pin and level cleared and `precision_ok` set to 0. A machine-scope lesson stays `promoted`, because machine scope cannot be a candidate, but its pin is cleared, its level returns to `note`, and `precision_ok` is set to 0 (R06, R27) | human, through the `e` key in `scars inbox` | 1 |
| `muted` | any active | retired | human | 1 |
| `retired` | any active | retired | human | 1 |
| `superseded` | any active | superseded, with `superseded_by` set | human, `scars supersede` | 2 |

`retired` and `superseded` are terminal. Reviving one means proposing a successor, which starts as a candidate. Every transition is one transaction: a compare-and-set update of the `lessons` row, plus one `events` row whose `prev_hash` extends the chain. A stale revision returns a conflict, and the CLI asks the human to retry.

Phase 2 adds `quarantined` for rolling back everything one actor proposed. Phase 1 handles a bad lesson with `scars retire` or `scars mute`.

## Scope and identity

- **Machine.** `machine_id` is a random UUID written by `scars init` to `~/.scars/machine`. Machine-scope lessons are always human-promoted, and `scars export` leaves them out unless `--include-machine` is passed (C21).
- **Repo.** `scars init`, and later `scars repo add`, register each clone. The hook maps a session's `cwd` to `git rev-parse --git-common-dir` and caches the result in `telemetry.db` `cwd_repo`, refreshed after 24 hours. All worktrees of a clone share one common dir. That was verified for the 52 `openwhispr` worktrees. Separate clones join a repo only when the user registers them. Forks register as separate repos (C21).
- **Owner class.** `repos.owner_class` is `own` or `upstream`. `scars init` sets `upstream` when the `origin` owner differs from the user's GitHub login, and the user can change it. Pairs mined in `upstream` repos get trust class `third_party_repo` (C13).
- **Path.** A path glob matches relative to `git rev-parse --show-toplevel`, case-insensitively, after resolving symlinks.
- **Session.** Session-scoped candidates are visible only to the `session_id` from hook stdin.

## Delivery stages

ADR-0001 defines three stages.

| Stage | Lessons eligible | Matching | Cap | Dedupe |
| --- | --- | --- | --- | --- |
| Plan, at session start and after compaction | promoted and pinned, scope machine or the session's repo | none, all eligible lessons ranked by score | 600 chars | once per session per context epoch |
| Plan, at UserPromptSubmit | promoted and pinned (R22), with a `prompt` or `path` trigger | prompt keywords, and paths named in the prompt | 2 lessons, 400 chars | once per session per context epoch |
| Gate, at PreToolUse | promoted, level `block` (Phase 2) | `command` or `path` trigger | 1 deny, 300 chars | never. Every matching call is denied (R07) |
| Repair, after a failed call | promoted or provisional with an `error` trigger, plus session-scoped candidates | error signature | 2 lessons, 400 chars | once per session per context epoch |

The context epoch starts at 0 and goes up by 1 on each compaction event. So after compaction, pinned lessons are injected again (C16, R07). Dedupe uses `insert or ignore` into `shown`. If `telemetry.db` cannot be written, the hook delivers anyway, and the duplicate costs at most one extra line (R02).

## Adapters

### Harness event map

| Stage | Claude Code 2.1.299 | Codex 0.162.1 |
| --- | --- | --- |
| Plan | `SessionStart` (sources `startup`, `resume`, `clear`, `compact`), `UserPromptSubmit`, `PostCompact` | `SessionStart` (matcher includes `compact`), `UserPromptSubmit` |
| Gate | `PreToolUse`, matcher `Bash\|Edit\|Write\|MultiEdit` | `PreToolUse`, matcher `Bash\|apply_patch`. Codex reports `exec_command` as `Bash` (R05) |
| Repair | `PostToolUseFailure` | `PostToolUse`, where the adapter reads the exit code from the tool response. The field path is pinned by a golden fixture |

Claude Code's `PostToolUse` fires only on success, so Claude Code repair uses `PostToolUseFailure` only (C02). Codex `SessionEnd` is capped at 3 s and is not used (ADR-0005). The Claude Code event names are in `research/experiments/evidence/claude-hooks-excerpt.md`. The Codex names come from the harness and Gemini reviewers' fetches of the Codex hooks docs and must be confirmed by golden fixtures before Phase 1 ships.

### Normalized event

```python
@dataclass(frozen=True)
class HookEvent:
    harness: Literal["claude", "codex"]
    harness_version: str
    event: Literal["session_start", "compact", "prompt", "pre_tool", "tool_failed"]
    session_id: str
    tool_use_id: str | None
    cwd: str
    tool_kind: Literal["shell", "edit", "other"] | None
    command: str | None          # shell command, capped at 4 KB
    paths: tuple[str, ...]       # from file_path, or parsed from apply_patch headers
    error_text: str | None       # failure output, capped at 4 KB
    prompt: str | None           # prompt text, capped at 4 KB
    model_id: str | None         # when the harness provides it
```

Each adapter has golden stdin and stdout fixtures in `tests/fixtures/<harness>/<version>/<event>.json`. CI replays them. `scars doctor` compares the installed harness version with the newest fixture version and warns on a mismatch (C19). Cursor also loads Claude Code hooks, so Cursor sessions could fire twice. The adapter skips any event whose stdin has a `cursor_version` field until a Cursor adapter exists.

### Output

- Context: `{"hookSpecificOutput": {"hookEventName": <event>, "additionalContext": <text>}}`.
- Gate (Phase 2): `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": <text>}}`.
- Human notice at session start: the top-level `systemMessage` field, which the user sees and the model does not. It is used for "3 scars lessons waiting: run scars inbox" and for health warnings (PRD FR-5, R09).
- Nothing else, ever. No `allow`, `ask`, `updatedInput`, `continue`, or `decision` (C09).

## Hook contract

1. First, check `~/.scars/OFF` and `SCARS_OFF`. If either is set, exit 0 with no output.
2. All work runs inside one try block. On any exception, write an `error` hit if possible, print nothing, and exit 0 (C08).
3. Open `telemetry.db` with `busy_timeout = 0` and read three things: the `cwd_repo` row for this `cwd`, the session's `context_epoch`, and the session's `shown` rows for that epoch. If the file is locked or missing, continue with no cache (resolve the repo with `git rev-parse`), epoch 0, and no dedupe (R19).
4. Open `lessons.db` with `mode=ro` and a 200 ms busy timeout. On failure, output nothing.
5. Parse, match and render. Matching has a 50 ms wall-clock deadline via `signal.setitimer`. On timeout, use the matches found so far and record `error_class = "match_deadline"`.
6. Print the output and flush stdout.
7. Write the hit, the new `shown` rows, a new `cwd_repo` row if one was resolved, and the epoch increment on compaction events, in one transaction on `telemetry.db` with `busy_timeout = 0`. If the database is busy, drop the writes. No step waits on a telemetry lock (R02).
8. Every hook entry in the plugin sets `"timeout": 2`. Claude Code's default is 600 s.
9. Nothing goes to stderr. Debug output goes to `~/.scars/logs/hook.log` only when `SCARS_DEBUG=1`.

The latency budget covers the whole process, from start to exit, including step 7 (NFR-1).

**Privileged commands (R21).** This is the one list that ADR-0003, the PRD and the agent deny rule all refer to: `scars promote`, `scars pin`, `scars unpin`, `scars level`, `scars retire`, `scars mute`, `scars supersede`, `scars off`, `scars on`, `scars import-guard`, `scars verify` and `scars quarantine`. Each refuses to run unless stdin and stdout are terminals, the parent process is not a known harness, and `CLAUDECODE` and `CODEX_SANDBOX` are unset. `scars inbox` runs the same checks before it applies any decision.

**Agent deny rule.** Independent of any lesson, the PreToolUse adapter denies agent shell calls that run `scars inbox` or any privileged command, and agent writes or edits under `~/.scars`. This works only as defense in depth (ADR-0003).

## Matching

Phase 1 has no regular expressions. That removes ReDoS by construction (C12, R12).

- **`command`** specs are space-separated tokens matched against one parsed simple command. `xcodebuild` matches when the program is `xcodebuild`. `xcodebuild -scheme *` requires the `-scheme` flag followed by any one argument. `!-showsdks` requires that the flag be absent. `**` matches any run of arguments. Arguments compare by exact string. A token ending in `*` is a prefix match.
- **`path`** specs are anchored globs relative to the repo root, using `fnmatch` with `**` support.
- **`error`** specs are an exact error signature from `signature.py`.
- **`prompt`** specs are a set of lowercase keywords. All of them must appear as words in the prompt.

**Shell parsing.** `shlex.shlex(cmd, posix=True, punctuation_chars=True)` splits the command into tokens and the operators `&&`, `||`, `;`, `|`, `&`, `(`, `)`, `<`, `>` and `>>`. Simple commands are the token runs between `&&`, `||`, `;`, `|` and `&`. Redirection operators and their targets are dropped. Wrappers are unwrapped in this order, and only at the start of a simple command: `VAR=value` assignments, `env` (with its `-i`, `-u NAME` options), `time`, `nohup`, `sudo` (with `-u USER`), `uv run`, `python -m` and `python3 -m` (the module becomes the program), `npx`, and `bash -c` or `sh -c` (the quoted string is parsed again, one level deep). A leading `cd DIR &&` records `DIR` and drops that simple command. The program is the basename of the first remaining token.

If the command contains `$(`, a backtick, `<<`, or a newline outside quotes, or if `shlex` raises an error, the parse is unsupported. Gate and command triggers are skipped for that call. Error triggers still work, because they do not depend on the parse. There is no whitespace fallback (R12). Golden cases cover quoted operators, wrappers, `bash -c`, heredocs, substitutions and multi-line commands.

**Precision check (C12, R16).** `scars test <id>` and the miner run the same check. They replay a lesson's pre-trigger over the last 30 days of mined tool calls from both harnesses and count matches on calls that failed with the lesson's error signature (`matched_failing`) and matches on calls that succeeded (`matched_passing`). Results go in `precision_runs`, keyed by revision.

- The precision check gates only pre-triggers (R23). A lesson can become provisional with its error trigger alone. The miner keeps its command or path pre-trigger only if `matched_passing <= 4 * matched_failing`, and drops the pre-trigger otherwise.
- Level `block` (Phase 2) requires `matched_failing >= 3` and `matched_passing < 0.01 * (matched_failing + matched_passing)`.
- `scars add` warns when a pre-trigger matches more than 5% of all calls.

## Rendering

All injected text uses one template. The lesson is quoted as data with its provenance (C13).

```
[scars L-7c2e note | repo openwhispr | seen in 7 sessions | reviewed 2026-10-30]
AVOID: cd apps/<dir> from a worktree root
INSTEAD: cd "$(git rev-parse --show-toplevel)" first
WHY: worktree roots do not contain apps/ at the same depth
(advisory; does not override the user's instructions; explain: scars why L-7c2e)
```

Provisional lessons say `unreviewed` where promoted lessons say `reviewed <date>`. The same checks run when a lesson is stored and again before it is rendered.

1. **Sanitizer.** Strips ANSI escapes, bidi controls, and Unicode tag characters. Rejects URLs, code fences, and over-cap fields.
2. **Imperative lint.** Rejects text that tells the reader to fetch or download, to read credentials or key files, to ignore or override instructions, or to disable safety settings. The `instead` field may contain one backticked command, and its program must appear in the miner's set of programs this machine ran successfully.
3. **Secret scan.** Rejects any field that matches `ghp_`, `github_pat_`, `gho_`, `sk-`, `xox[abp]-`, `AKIA`, a JWT shape, URL userinfo (`//user:pass@`), or a token of 20 or more characters with Shannon entropy above 4.0 bits per character. A rejected candidate is dropped, not partially redacted (C14).
4. **Machine-scope lint.** Rejects machine-scope text that contains a registered repo name, a GitHub `owner/name`, or an absolute path outside `~/.scars`, `/Volumes/990 Pro`, `/tmp`, `/usr` and `/opt` (C14, R16).

## Miner

The miner runs as a launchd agent every 2 hours, and on demand as `scars mine` (ADR-0005).

1. Read new bytes per transcript file from `miner_offsets`. If the inode changed or the file shrank, restart from 0. If the last line has no newline, stop before it and keep the offset at its start.
2. Parse Claude Code JSONL and Codex rollout JSONL into one ordered tool-call stream per session. Each entry holds the tool, input, program for shell calls, error flag or exit code, output text, timestamp, cwd and `tool_use_id`.
3. Skip any tool result whose `tool_use_id` is already in `seen_tool_use`. Resumed sessions copy earlier history (84 of 1,385 errors in the probe).
4. For each failed call, compute the signature with `signature.py`. Skip unlessonable classes (timeouts, hook denials, user rejections, auto-mode classifier denials, interrupts) and signatures without a word of 3 or more letters.
5. **Pair rule (C17, R11).** Look ahead up to 10 calls in the same session for the first successful call with the same tool and, for shell calls, the same program. If any `Edit`, `Write`, `MultiEdit`, `NotebookEdit` or `apply_patch` call comes first, there is no pair. A failure that reaches the end of a batch without a decision goes into `pending_failures`, in the same transaction that advances `miner_offsets`. The next batch continues its look-ahead from `calls_since`. It is dropped once it has seen 10 more calls or the session has been idle for 24 hours. `seen_tool_use` records only results that have been fully processed, so a pending failure's later calls are still read.
6. **Fingerprint (R09).** `sha256(scope_kind, repo_id or "", path_glob or "", session_id if scope_kind is session else "", error_signature)`. Fingerprints use the signature and not the command text, so two sessions that hit the same error in the same repo merge into one lesson even if their commands differ. Mined lessons start with scope `repo`. A human can widen one to machine scope at promotion.
7. Build or update the lesson. A new fingerprint creates a candidate. `avoid` is the failed input, reduced to the program and the arguments that differ from the passing input. `instead` is the passing input reduced the same way. `reason` is the signature. Triggers are the `error` trigger plus a `command` pre-trigger made from the program and its differing arguments. An existing fingerprint gets an `observed` event, adds the fix program to `fix_programs`, and resets the expiry.
8. Run the sanitizer, lints and secret scan. Drop the candidate on any rejection.
9. Write an `observations` row.
10. Apply ADR-0004. Provisionalize own-code, repo-scope candidates that passed step 8. Always keep their error trigger. Keep their command or path pre-trigger only if it passes the precision check in "Matching" (R23). Expire candidates and provisional lessons with no sighting for 14 days.
11. **Outcome pass (R08, R24).** For each hit in `telemetry.db` without an outcome, find the hit's reference call. For a repair hit, that is the failed call that fired it. For a plan-stage hit, it is the first later call in the session that matches the lesson's pre-trigger, if any. Then apply the first rule that holds, in this order:
    1. `ignored_then_failed` if the lesson's error signature occurs at the reference call or within 5 calls after it. For a repair hit, the reference call is the failure that fired the hit, so it does not count, and only the 5 calls after it do. For a plan-stage hit, a failure at the reference call itself counts.
    2. `fired_unneeded` for a plan-stage hit whose reference call succeeded and no failure with the lesson's error signature followed within 5 calls.
    3. `heeded` for a repair hit when the next call of the same tool kind succeeded or no longer matches the pre-trigger.
    4. `no_attempt` for a plan-stage hit when the session ended, or went idle for 24 hours, without any call matching the pre-trigger.
    5. `unknown` when the session ended, or went idle for 24 hours, before any rule could be decided.

    Rules 2 to 4 are decided only after the 5-call window after the reference call has closed, or the session has ended or idled for 24 hours. That way a later failure always takes precedence, and the result does not depend on when the miner ran. A hit stays without an outcome until then. Repair hits cannot be `fired_unneeded`, because they fire only after a failure. The pass also backfills `model_id` from the transcript.

Phase 1 writes `avoid`, `instead` and `reason` by template, with no LLM. The inbox lets the human rewrite them.

## Verification (Phase 2)

Repro evidence is `{"argv": [...], "expect": "fail" | "pass"}` (C04). Only programs on an allowlist may run: `git`, `python3`, `uv`, `npm`, `pytest`, `swiftc`, `xcrun`, and user-added entries. `scars verify <id>` shows the argv and asks for confirmation. Then it runs the argv under `sandbox-exec` with a profile that denies network and allows writes only to a temporary worktree from `git worktree add --detach`. It uses a scrubbed environment, a temporary `HOME`, and a 120 s timeout. The result is stored bound to the lesson revision, the argv hash, the tree SHA, and an environment fingerprint (Python, Xcode and macOS versions) (C22). Editing the lesson invalidates it.

## Install and distribution

- **Claude Code.** A plugin whose `hooks/hooks.json` registers the events in the event map, each with `"timeout": 2`. Install and removal go through the plugin commands, so `~/.claude/settings.json` is never hand-merged. That file is co-managed by another tool (`managed_by=headroom`) (C20).
- **Codex.** A plugin with its own `hooks.json`. Codex records trust against the hook definition's hash, so the command string stays stable across upgrades: an absolute path to the shim. `scars doctor` reports when Codex has not trusted the hook (C20).
- **Code.** The shim `~/.scars/bin/scars-hook` execs a versioned install in `~/.scars/versions/<version>/`, outside every agent workspace. Phase 2 adds `chflags uchg` and a manifest hash check (THREAT-MODEL T8). Phase 1 leaves them out, because a same-user process can undo both and the agent deny rule covers the common case (R14).
- **Public release (Phase 3).** pipx for the CLI, plus the two plugins. No repo-level hook config is ever written into a working tree.

## Telemetry and doctor

`scars doctor` reads the last 24 hours of `hits` and reports, for each harness:

- the last time the hook was seen, p50 and p99 latency, and error classes,
- the kill-switch state, plugin install and trust state, and whether the sandbox denies writes to `~/.scars` (ADR-0003),
- the store location check (internal disk, not a network or synced folder),
- the result of the event hash-chain walk.

It also runs a canary. It feeds a synthetic Claude Code `PostToolUseFailure` payload to `scars hook` and requires the built-in canary lesson in the output within 100 ms (C08). Session start shows a `systemMessage` to the human when the last 24 hours have an error rate above 1%.

## Fault tests

Each test asserts a literal outcome (C07, C27).

- Two processes run compare-and-set updates on one lesson at the same revision. Exactly one changes a row, and the other reports a conflict.
- Writers get `SIGKILL` at random points during 1,000 lifecycle changes. After restart, every change the CLI reported as committed is present, `PRAGMA integrity_check` returns `ok`, and the hash chain verifies.
- `lessons.db` is made unreadable with `chmod 000`, then replaced by a corrupt file. Each hook run exits 0 with empty stdout in under 250 ms.
- `telemetry.db` is locked by a long write transaction. The hook still prints the correct lesson, and the hit is dropped.
- A truncated last line in a transcript leaves the offset at that line's start, and the next batch reads it once complete.
- Golden shell cases: `a && b`, `"a && b"` in quotes, `bash -c 'xcodebuild'`, `uv run pytest`, `$(xcodebuild)`, and a heredoc. Unsupported forms produce no command-trigger match.
- 64 concurrent fresh-process hook runs against 1,000 lessons on the `990 Pro` volume, with the telemetry write included, keep p99 at 50 ms or less.

## Performance budget

| Step | Budget | Measured |
| --- | --- | --- |
| Python start and imports | 22 ms | 19.5 to 20.1 ms p50 with an empty store (`research/experiments/hook-latency.json`) |
| Open `lessons.db` and run the indexed query, 1,000 lessons | 5 ms | 23.1 to 23.9 ms p50 for start, open, query and regex together (`research/experiments/sqlite-latency-*.json`). Subtracting the start time gives about 3 ms, which is an inference across two scripts |
| Shell parse and match | 8 ms | not measured |
| Render and print | 3 ms | not measured |
| Telemetry read before matching and write after output, no lock waits | 4 ms | not measured |
| Total p99, no match | 50 ms | not measured end to end. The probe without parsing or the telemetry write reached p99 24.4 to 38.1 ms (R17) |

Phase 1 acceptance requires `scars bench` to measure the whole hook, including the telemetry write, under the fault-test load. If p99 exceeds 50 ms, the first fix is lazy imports. The second is a long-lived helper on a Unix socket with direct reads as the fallback. That adds a daemon, so it is built only if the measurement requires it.

## File layout

```
~/.scars/
  lessons.db       lessons, triggers, observations, events, offsets (0600)
  telemetry.db     hits, outcomes, shown, epochs, cwd cache (0600)
  machine          machine_id
  OFF              kill switch: present means off
  bin/scars-hook      stable shim referenced by the plugins
  versions/<v>/    pinned install
  logs/            debug and miner logs
~/Library/LaunchAgents/dev.scars.miner.plist
```
