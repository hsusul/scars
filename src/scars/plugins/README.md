# Hook configuration that `scars init` installs

`scars init` renders these files, replacing `@SCARS_HOOK@` with the shell-quoted absolute path of the installed `scars-hook` script, and `@SCARS@` and `@LOGS@` in the launchd job. It shows a diff and asks before it writes anything.

- `.claude-plugin/marketplace.json` and `claude/` form a local Claude Code marketplace holding the `scars` plugin. `scars init` copies them to `~/.scars/plugins/`, then prints the two commands that add the marketplace and install the plugin. It never edits `~/.claude/settings.json`. The plugin's `hooks/hooks.json` registers `SessionStart`, `UserPromptSubmit`, `PreToolUse` on `Bash|Edit|Write|MultiEdit` and `PostToolUseFailure`.
- `launchd/dev.scars.miner.plist` runs `scars mine` every 2 hours. `scars init` writes it to `~/Library/LaunchAgents/` and prints the `launchctl bootstrap` command that loads it.

Every hook sets `"timeout": 2`, in seconds. Claude Code otherwise waits up to 600 seconds.

On upgrades, `scars init` also offers to remove the exact scars commands older versions installed in `~/.codex/hooks.json`. It keeps foreign hooks and skips absent files or files without scars entries. `--yes` accepts the cleanup.

## Sources

The JSON shapes follow these pages, fetched on 2026-10-30. The pages are the English documentation at code.claude.com.

- Claude Code `plugins-reference.md`: the plugin manifest, and the `hooks` wrapper a hooks file needs.
- Claude Code `hooks.md`: hook events, matchers and the `timeout` field.
- Claude Code `plugins/marketplace-reference.md`: the marketplace file, where `name`, `owner` and `plugins` are required.
