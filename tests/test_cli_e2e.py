"""From an empty machine to a lesson in the hook's output: init, mine, inbox, hook."""

import json
import os
import subprocess
import time

from cli_helpers import FAILURE_PAIR, SIG, claude_transcript, git_repo, make_ctx, run
from scars import store

pytest_plugins = ("cli_fixtures",)


def test_init_mine_inbox_then_the_hook_shows_the_lesson(work, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    user_home = work / "user"
    scars_home = user_home / ".scars"
    monkeypatch.setenv("SCARS_HOME", str(scars_home))
    for name in ("SCARS_OFF", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    repo = git_repo(work / "app")
    transcript = user_home / ".claude" / "projects" / "app" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(claude_transcript(str(repo), time.time() - 3600, FAILURE_PAIR))

    code, out, err = run(make_ctx(user_home, clock=time.time), "init", "--yes", "--allow-dev-install")
    assert (code, err) == (0, "")
    assert oct(os.stat(scars_home).st_mode & 0o777) == "0o700"
    assert (scars_home / "lessons.db").exists() and (scars_home / "telemetry.db").exists()
    assert "repo app: registered as app (own)\n" in out
    assert "read 1 transcripts: 2 calls, 1 failures, 1 pairs\n" in out
    assert "lessons: 1 new, 1 observations, 1 provisional, 0 expired\n" in out
    assert "would have fired 1 time in the last 30 days\n" in out
    for path in (
        scars_home / "plugins" / ".claude-plugin" / "marketplace.json",
        scars_home / "plugins" / "claude" / "hooks" / "hooks.json",
        user_home / ".codex" / "hooks.json",
        user_home / "Library" / "LaunchAgents" / "dev.scars.miner.plist",
    ):
        assert path.exists(), path
    assert not (user_home / ".claude" / "settings.json").exists()

    code, out, err = run(make_ctx(user_home, keys="a", clock=time.time), "inbox")
    db = store.connect(scars_home / "lessons.db")
    [lesson] = store.list_lessons(db)
    db.close()
    assert (code, err, lesson.status.value) == (0, "", "promoted")
    assert f"promoted {lesson.id} (repo app, level note, not pinned)\n" in out

    hooks = json.loads((scars_home / "plugins" / "claude" / "hooks" / "hooks.json").read_text())
    command = hooks["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]
    assert command == f"{scars_home}/bin/scars-hook claude PostToolUseFailure"
    payload = {
        "session_id": "next-session",
        "cwd": str(repo),
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest --frobnicate tests/unit"},
        "tool_use_id": "toolu_next",
        "error": f"Exit code 4\n{SIG}",
    }
    result = subprocess.run(
        ["/bin/sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True, timeout=10
    )
    assert (result.returncode, result.stderr) == (0, "")
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"[scars {lesson.id} note | repo app | seen 1x | reviewed " in context
    assert "pytest --frobnicate" in context

    code, out, _ = run(make_ctx(user_home), "why", "last")
    assert out.startswith(f"{lesson.id}  promoted  level note  not pinned  repo app")
