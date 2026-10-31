"""The human-only check that privileged commands run before they change anything."""

import os

import pytest

from cli_helpers import make_ctx, run
from scars import commands, paths
from scars.commands import ANCESTOR_DEPTH, harness_in, process_ancestors, refusal

pytest_plugins = ("cli_fixtures",)

UNKNOWN = "refused: cannot tell which processes started it; run it in your own terminal"


def test_ancestors_come_from_an_absolute_ps_whatever_path_says(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    found = process_ancestors(os.getpid())
    assert found is not None
    assert "pytest" in found[0]


def test_off_refuses_when_ps_cannot_run(home, user_home, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(commands, "PS", "/nonexistent/ps", raising=False)
    ctx = make_ctx(user_home)
    ctx.ancestors = process_ancestors
    assert run(ctx, "off") == (1, "", f"scars off: {UNKNOWN}\n")
    assert not paths.kill_switch().exists()


def test_ps_that_fails_gives_no_ancestry(monkeypatch, tmp_path):
    fake = tmp_path / "ps"
    fake.write_text("#!/bin/sh\necho '  1 /bin/zsh'\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(commands, "PS", str(fake), raising=False)
    assert process_ancestors(os.getpid()) is None


def test_ps_with_empty_output_gives_no_ancestry(monkeypatch, tmp_path):
    fake = tmp_path / "ps"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(commands, "PS", str(fake), raising=False)
    assert process_ancestors(os.getpid()) is None


@pytest.mark.parametrize("found", [None, []], ids=["unknown", "empty"])
def test_refusal_when_ancestry_is_unknown_or_empty(user_home, found):
    ctx = make_ctx(user_home)
    ctx.ancestors = lambda: found
    assert refusal(ctx) == UNKNOWN.removeprefix("refused: ")


def chain_ps(tmp_path, top):
    """A fake `ps` where every pid's parent is pid - 1 running `/bin/sh`, and pid 2 is `top` under pid 1."""
    fake = tmp_path / "ps"
    fake.write_text(
        f'#!/bin/sh\npid="$5"\nif [ "$pid" -le 2 ]; then echo "    1 {top}"; '
        'else echo "$((pid - 1)) /bin/sh"; fi\n'
    )
    fake.chmod(0o755)
    return str(fake)


def test_a_chain_deeper_than_the_walk_gives_no_ancestry(home, user_home, monkeypatch, tmp_path):
    # An agent nests waiting subshells until the harness is past the walk's depth.
    monkeypatch.setattr(commands, "PS", chain_ps(tmp_path, "claude"))
    start = ANCESTOR_DEPTH + 3
    assert process_ancestors(start) is None
    ctx = make_ctx(user_home)
    ctx.ancestors = lambda: process_ancestors(start)
    assert run(ctx, "off") == (1, "", f"scars off: {UNKNOWN}\n")
    assert not paths.kill_switch().exists()


def test_a_chain_that_reaches_pid_1_within_the_walk_is_read_whole(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, "PS", chain_ps(tmp_path, "-zsh"))
    assert process_ancestors(ANCESTOR_DEPTH + 1) == ["/bin/sh"] * (ANCESTOR_DEPTH - 1) + ["-zsh"]


HARNESS_LINES = [
    # Remy, an agent app: its backend and the app itself.
    "/Applications/Remy.app/Contents/Resources/backend/remy/remy run-module remy.host.core_host",
    "/Applications/Remy.app/Contents/MacOS/Remy",
    "/Users/u/Applications/Remy.app/Contents/MacOS/Remy",
    "remy run-module remy.host.core_host",
    # Claude Code: native installer, npm's native binary, npm's node entry, the desktop app.
    "/Users/u/.local/share/claude/versions/2.0.14 --resume",
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    "/usr/local/bin/node --no-warnings /usr/local/bin/claude -p hi",
    "/Applications/Claude.app/Contents/MacOS/Claude",
    # Codex: npm's node entry, its native binary, the app-bundled CLI, Homebrew's cask binary.
    "node /opt/homebrew/bin/codex exec",
    "/opt/homebrew/lib/node_modules/@openai/codex/vendor/aarch64-apple-darwin/codex/codex exec",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server",
    "/Applications/Codex.app/Contents/MacOS/Codex",
    "/opt/homebrew/Caskroom/codex/0.50.0/codex-aarch64-apple-darwin",
    "bun /Users/u/.bun/install/global/node_modules/@openai/codex/bin/codex.js",
]


@pytest.mark.parametrize("line", HARNESS_LINES)
def test_real_harness_processes_are_recognized(home, user_home, line):
    assert harness_in(line) is True
    ctx = make_ctx(user_home, ancestors=["/bin/sh -c scars off", line])
    assert run(ctx, "off") == (
        1,
        "",
        "scars off: refused: it was started from an agent harness; run it in your own terminal\n",
    )


@pytest.mark.parametrize(
    "line",
    [
        "vim /tmp/claude-code/notes.txt",
        "/usr/bin/less /usr/local/lib/node_modules/@openai/codex/README.md",
        "/bin/zsh -c cd /Applications/Remy.app/Contents && ls",
        "tmux new -s claude",
        "node /Users/u/src/remy-clone/server.js",
        "python3 -c import codex",
        "/usr/bin/vim /Applications/Claude.app/Contents/Info.plist",
    ],
)
def test_a_harness_word_outside_the_executable_is_not_a_harness(home, user_home, line):
    assert harness_in(line) is False
    assert run(make_ctx(user_home, ancestors=[line, "-zsh"]), "off")[0] == 0
    assert paths.kill_switch().exists()


@pytest.mark.parametrize("variable", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_an_agent_variable_set_to_empty_still_refuses(home, user_home, variable):
    ctx = make_ctx(user_home, env={variable: ""})
    assert run(ctx, "off") == (
        1,
        "",
        f"scars off: refused: {variable} is set, so this looks like an agent session; "
        "run it in your own terminal\n",
    )
    assert not paths.kill_switch().exists()
