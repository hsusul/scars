# ADR-0001: Deliver lessons in three stages, and never as a non-blocking PreToolUse hint

Status: accepted, 2026-10-29, revised after review round 2. Answers C01, C02 and R05. The docs excerpt this ADR relies on is saved in `research/experiments/evidence/claude-hooks-excerpt.md`.

## Context

The research README argued that a hint shown at PreToolUse would stop an agent before it repeated a mistake. That is false for the primary harness. By the time PreToolUse fires, the model has already spent a turn choosing the call. Claude Code delivers PreToolUse `additionalContext` "alongside the tool result", which means after the tool has run (Claude Code hooks reference, `hooks.md` line 1964, Claude Code 2.1.299). Only `permissionDecision: "deny"` acts before execution. A non-deny hint on `xcodebuild` would let the build that froze the Mac run anyway, with a note attached.

Claude Code also reports failures on `PostToolUseFailure`. `PostToolUse` fires only on success.

## Decision

A lesson reaches an agent at one of three stages. Each stage has one job.

| Stage | Goal | Claude Code event | Codex event | Output |
| --- | --- | --- | --- | --- |
| Plan | Prevent the attempt | `SessionStart`, `UserPromptSubmit`, `PostCompact` | `SessionStart` (including the `compact` source), `UserPromptSubmit` | `additionalContext` |
| Gate (Phase 2) | Stop the execution | `PreToolUse` | `PreToolUse` with matcher `Bash\|apply_patch` | `permissionDecision: "deny"` with the lesson as the reason, and nothing else |
| Repair | Shorten recovery after a failure | `PostToolUseFailure` | `PostToolUse` with a non-zero exit code | `additionalContext` |

Human-facing notices, such as "lessons waiting for review", use the top-level `systemMessage` field, which the user sees and the model does not.

PreToolUse returns a deny or nothing. It never returns `additionalContext` alone, `allow`, `ask`, or `updatedInput`.

## Consequences

- The value claim changes. Plan-stage lessons can prevent attempts. Gate lessons turn a harmful execution into one cheap denied call. Repair lessons cut the turns spent recovering. The eval measures harmful executions and turns lost, not attempts.
- Plan-stage injection must stay small and specific, because the ETH AGENTS.md study found that broad repository overviews hurt while specific instructions are followed (C15). The budget is set in PRD NFR-4.
- Gate lessons need the strongest controls, because a wrong deny stops work. Only human-promoted lessons can deny, and only from Phase 2, after the sandbox check passes (ADR-0003, ADR-0004).
- In Phase 1 the PreToolUse hook exists only for the agent deny rule that protects scars's own commands and files (ARCHITECTURE "Hook contract").
- The live probe (`research/experiments/probe_pretooluse_context.sh`) is still to be run once Claude Code is logged in again. If it shows the context arriving before execution, this ADR is revisited.
