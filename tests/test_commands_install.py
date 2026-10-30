import json
import os
import plistlib
import shlex
import time

import pytest

from cli_helpers import FAILURE_PAIR, claude_transcript, git_repo, make_ctx, run
from scars import store
from scars.commands import install

pytest_plugins = ("cli_fixtures",)

HOOK = "/opt/scars bin/scars-hook"
QUOTED = shlex.quote(HOOK)


@pytest.fixture
def machine(work, monkeypatch):
    """A temp user home with one transcript in a git repo, and SCARS_HOME inside it."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    user = work / "user"
    scars_home = user / ".scars"
    monkeypatch.setenv("SCARS_HOME", str(scars_home))
    for name in ("SCARS_OFF", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(install, "installed_script", lambda name: f"/opt/scars bin/{name}")
    return user, scars_home


def transcript(user, cwd, name="s1"):
    path = user / ".claude" / "projects" / "p" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(claude_transcript(str(cwd), time.time() - 3600, FAILURE_PAIR))


def test_claude_plugin_hooks_match_the_documented_shape():
    config = install.hooks_config("claude/hooks/hooks.json", HOOK)
    assert list(config) == ["hooks"]
    events = config["hooks"]
    assert list(events) == ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUseFailure"]
    for event, groups in events.items():
        [group] = groups
        assert group.get("matcher") == ("Bash|Edit|Write|MultiEdit" if event == "PreToolUse" else None)
        assert group["hooks"] == [{"type": "command", "command": f"{QUOTED} claude {event}", "timeout": 2}]
    manifest = json.loads(install._template("claude/.claude-plugin/plugin.json"))
    assert manifest["name"] == "scars"
    market = json.loads(install._template(".claude-plugin/marketplace.json"))
    assert (market["name"], market["owner"]["name"], market["plugins"]) == (
        "scars-local",
        "scars",
        [{"name": "scars", "source": "./claude"}],
    )


def test_codex_hooks_match_the_documented_shape():
    events = install.hooks_config("codex/hooks.json", HOOK)["hooks"]
    assert list(events) == ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"]
    for event, [group] in events.items():
        assert group.get("matcher") == (
            None if event in ("SessionStart", "UserPromptSubmit") else "Bash|apply_patch"
        )
        assert group["hooks"] == [{"type": "command", "command": f"{QUOTED} codex {event}", "timeout": 2}]


def test_codex_merge_keeps_other_hooks_and_replaces_old_scars_entries():
    existing = {
        "description": "mine",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/guard.py"}]},
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/old/scars-hook codex PreToolUse"}],
                },
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "/old/scars-hook codex Stop"}]}],
        },
    }
    ours = install.hooks_config("codex/hooks.json", HOOK)
    merged = install.merge_codex(existing, ours)
    assert merged["description"] == "mine"
    assert merged["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/guard.py"}]},
        *ours["hooks"]["PreToolUse"],
    ]
    assert "Stop" not in merged["hooks"]
    assert install.merge_codex(merged, ours) == merged


def test_init_yes_is_idempotent(machine, work):
    user, scars_home = machine
    transcript(user, git_repo(work / "app"))
    first = run(make_ctx(user, clock=time.time), "init", "--yes")
    assert first[0] == 0
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes")
    plist = user / "Library" / "LaunchAgents" / "dev.scars.miner.plist"
    assert (code, err) == (0, "")
    assert out.splitlines()[:9] == [
        f"store: {scars_home}",
        "repos: all 1 found are registered",
        "Claude Code plugin: unchanged",
        "to turn it on in Claude Code, run:",
        f"  claude plugin marketplace add {scars_home}/plugins",
        "  claude plugin install scars@scars-local",
        "Codex hooks: unchanged",
        "Codex asks you to review and trust the new hooks the next time it starts",
        "miner schedule: unchanged",
    ]
    assert "read 0 transcripts: 0 calls, 0 failures, 0 pairs" in out
    job = plistlib.loads(plist.read_bytes())
    assert (job["ProgramArguments"], job["StartInterval"], job.get("EnvironmentVariables")) == (
        ["/opt/scars bin/scars", "mine"],
        7200,
        None,
    )


def test_launchd_job_carries_a_non_default_home(tmp_path):
    job = install.launchd_file(tmp_path, "/bin/scars", tmp_path / "elsewhere", {"SCARS_HOME": "/x"})
    assert job.path == tmp_path / "Library" / "LaunchAgents" / "dev.scars.miner.plist"
    assert plistlib.loads(job.content.encode())["EnvironmentVariables"] == {"SCARS_HOME": "/x"}


def test_init_yes_skips_repos_with_a_remote(machine, work):
    user, scars_home = machine
    repo = git_repo(work / "lib", remote="https://example.com/x/lib.git")
    transcript(user, repo)
    out = run(make_ctx(user, clock=time.time), "init", "--yes")[1]
    assert f"repo lib ({repo}/.git): has a remote, so not sure it is yours; skipped\n" in out
    db = store.connect(scars_home / "lessons.db")
    assert db.execute("select count(*) from repos").fetchone()[0] == 0


def test_init_asks_per_repo_and_per_file_group(machine, work):
    user, scars_home = machine
    repo = git_repo(work / "lib", remote="https://example.com/x/lib.git")
    transcript(user, repo)
    ctx = make_ctx(user, lines="u\ny\nn\nn\n", clock=time.time)
    code, out, _ = run(ctx, "init")
    assert code == 0
    assert (
        f"register lib ({repo}/.git)? [o]wn, [u]pstream, [s]kip: repo lib: registered as lib (upstream)\n"
        in out
    )
    assert "write the Claude Code plugin? [y/N] Claude Code plugin: written\n" in out
    assert "write the Codex hooks? [y/N] Codex hooks: not written\n" in out
    assert "write the miner schedule? [y/N] miner schedule: not written\n" in out
    assert (scars_home / "plugins" / "claude" / "hooks" / "hooks.json").exists()
    assert not (user / ".codex" / "hooks.json").exists()
    assert not (user / "Library").exists()
    db = store.connect(scars_home / "lessons.db")
    assert db.execute("select id, display_name, owner_class from repos").fetchall() == [
        ("lib", "lib", "upstream")
    ]


def test_init_off_macos_writes_no_launchd_job(machine):
    user, _ = machine
    out = run(make_ctx(user, platform="linux", clock=time.time), "init", "--yes")[1]
    assert "miner schedule: launchd is macOS only; run `scars mine` every 2 hours from cron instead\n" in out
    assert not (user / "Library").exists()


def test_init_refuses_a_broken_codex_config(machine):
    user, _ = machine
    (user / ".codex").mkdir(parents=True)
    (user / ".codex" / "hooks.json").write_text("{not json")
    assert run(make_ctx(user, clock=time.time), "init", "--yes")[2] == (
        f"scars init: cannot parse {user}/.codex/hooks.json; "
        "fix it or move it away, then run scars init again\n"
    )


def test_store_directory_is_private(machine):
    user, scars_home = machine
    run(make_ctx(user, platform="linux", clock=time.time), "init", "--yes")
    assert oct(os.stat(scars_home).st_mode & 0o777) == "0o700"
    assert oct(os.stat(scars_home / "lessons.db").st_mode & 0o777) == "0o600"
