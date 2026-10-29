# ADR-0003: Privileged changes need a human at a terminal, enforced by the harness sandbox, with tamper detection

Status: accepted, 2026-10-29, revised after review round 2. Answers C03 and R01.

## Context

Every agent session runs as the same Unix user as the human. Any session can write any file the human can, unless a harness sandbox stops it. In the README design, `promoted` carried `by: "human"`, which any session could forge.

The first version of this ADR signed privileged events with an HMAC whose key sat in the macOS Keychain behind a confirmation prompt. The GPT reviewer found the flaw (R01). To check an HMAC, the hook must read the same key, so every hook run would raise a prompt, or the key would have to be readable without one, which defeats it. A signature scheme with a public verification key avoids that, but a same-user attacker who can edit the verifier, the public key file, or the hook code still wins. Without an operating-system boundary, signatures add complexity without adding protection.

## Decision

- **Boundary.** The harness sandbox. Claude Code and Codex both offer sandboxed modes that restrict writes to the workspace. `scars doctor` checks whether each harness's sandbox configuration denies writes to `~/.scars`, and it reports any harness where it does not.
- **Privileged commands.** The single list lives in ARCHITECTURE "Hook contract" under "Privileged commands" (R15). Each one refuses to run unless stdin and stdout are terminals, the parent process is not a known harness, and `CLAUDECODE` and `CODEX_SANDBOX` are unset. A determined agent can get around this check, so it is a speed bump, not a boundary.
- **Agent deny rule.** The PreToolUse adapter denies agent shell calls that run `scars inbox` or any privileged command, or that write under `~/.scars`. This is defense in depth.
- **Tamper detection.** Each `events` row stores `prev_hash = sha256(previous row's canonical JSON)`. `scars doctor` and the weekly digest walk the chain and recompute every lesson's state from its events. If the chain breaks or a `lessons` row disagrees with its events, they report it, and `scars doctor --repair` rebuilds the `lessons` rows from the events. Detection does not prevent tampering. It makes a silent edit visible within a day.
- **Phase 1 limits.** No lesson can reach level `block` in Phase 1. Plan-stage pins are human-only and capped at 600 characters in total. The worst a forged change can do in Phase 1 is inject capped, quoted advisory text, and the next `scars doctor` run reports it.
- **Teams.** Asymmetric signatures (a per-person private key with a hardware or Keychain prompt, and public keys checked by the sync service) belong to the team design, where the verifier runs outside the attacker's account.

## Consequences

- No Keychain prompts during normal use, and no key material on the hot path.
- The security claim is stated honestly. scars limits and detects same-user tampering. It does not prevent it. THREAT-MODEL T1 to T3 record the residual risk.
- Phase 2 block level requires the sandbox check to pass for every installed harness, because a deny is the most powerful thing a lesson can do.
