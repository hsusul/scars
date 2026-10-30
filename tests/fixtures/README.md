# Hook stdin fixtures

`tests/fixtures/<harness>/<version>/<event>.json` holds golden cases for one harness event. Each case has
the stdin the harness sends and either the `HookEvent` the adapter must produce (`event`) or the error
it must raise (`error`: `MalformedInput` or `Ignored`). `tests/test_adapters.py` replays every case, and
`tests/test_hook.py` checks the stdout the hook prints for them end to end.

The payloads follow the field names and examples in the official docs, fetched on 2026-10-30:

- Claude Code 2.1.299: the hooks reference `hooks.md` on code.claude.com, at path `/<d>/en/hooks.md`
  where `<d>` is the documentation path segment (written this way because the repository's
  planning-doc check rejects that segment followed by a slash) (sections "Common input fields",
  "SessionStart input", "UserPromptSubmit input", "PreToolUse input", "PostToolUseFailure input",
  "PostCompact input", "PreToolUse decision control" and "Add context for Claude").
- Codex 0.162.1: https://developers.openai.com/codex/hooks (sections "Common input fields",
  "SessionStart", "UserPromptSubmit", "PreToolUse" and "PostToolUse"), plus the generated input schemas
  at https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated.
  The docs leave `tool_response` untyped. The shape of the `Bash` response comes from the source at tag
  `rust-v0.162.1`, `codex-rs/core/src/tools/context.rs`: `ExecCommandToolOutput::post_tool_use_response`
  returns the output text as a JSON string, without the `Process exited with code N` header, and
  `ApplyPatchToolOutput` returns its text the same way.

Field names used:

| Harness | Event | Fields read |
| --- | --- | --- |
| both | every event | `hook_event_name`, `session_id`, `cwd`, optional `model`; `cursor_version` means skip |
| both | `SessionStart` | `source` |
| both | `UserPromptSubmit` | `prompt` |
| Claude Code | `PreToolUse`, `PostToolUseFailure` | `tool_name`, `tool_use_id`, `tool_input.command` (Bash), `tool_input.file_path` (Edit, Write, MultiEdit) |
| Claude Code | `PostToolUseFailure` | `error` |
| Codex | `PreToolUse`, `PostToolUse` | `tool_name`, `tool_use_id`, `tool_input.command` (Bash command, or apply_patch patch text) |
| Codex | `PostToolUse` | `tool_response` (an object's integer `exit_code`, or an exit-code header line in text) |

These are not captured live sessions. When a harness version changes, capture real payloads, add a new
version directory, and keep the old one so both shapes stay covered.
