"""From an empty machine to a lesson in the hook's output: init, mine, inbox, hook."""

import json
import os
import subprocess
import time

from cli_helpers import FAILURE_PAIR, SIG, claude_transcript, git_repo, iso, make_ctx, run
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
    assert "lessons: 1 new, 1 observations, 0 expired\n" in out
    assert "would have fired 1 time in the last 30 days\n" in out
    for path in (
        scars_home / "plugins" / ".claude-plugin" / "marketplace.json",
        scars_home / "plugins" / "claude" / "hooks" / "hooks.json",
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


SIM_TOOL = "mcp__Claude_Code_iOS_Simulator__control"
STALE = "D9C36E75-8409-4A69-9636-A6E35C689FBA"
LIVE = "FDCC5BE5-85E9-4BFE-A6FA-17A6BFCBA89E"
SIM_ERROR = (
    "No simulator with UDID {udid}. Pass a real device, or omit 'device' to use the attached or first "
    "booted simulator."
)


def tool_transcript(cwd: str, start: float) -> str:
    records: list[object] = []
    calls = [
        ("m1", {"action": "screenshot", "device": STALE}, SIM_ERROR.format(udid=STALE)),
        ("m2", {"action": "screenshot", "device": LIVE}, None),
    ]
    for i, (tool_id, args, error) in enumerate(calls):
        at = start + 10 * (i + 1)
        use = {"type": "tool_use", "id": tool_id, "name": SIM_TOOL, "input": args}
        records.append(
            {
                "type": "assistant",
                "timestamp": iso(at),
                "cwd": cwd,
                "message": {"model": "m", "content": [use]},
            }
        )
        result = {"type": "tool_result", "tool_use_id": tool_id, "is_error": error is not None}
        result["content"] = error if error is not None else "fine"
        records.append(
            {"type": "user", "timestamp": iso(at + 1), "cwd": cwd, "message": {"content": [result]}}
        )
    return "".join(json.dumps(r) + "\n" for r in records)


def test_a_mined_tool_lesson_reaches_the_hook_only_after_promotion(work, monkeypatch):
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
    transcript.write_text(tool_transcript(str(repo), time.time() - 3600))
    code, out, err = run(make_ctx(user_home, clock=time.time), "init", "--yes", "--allow-dev-install")
    assert (code, err) == (0, "")
    assert "lessons: 1 new, 1 observations, 0 expired\n" in out

    hooks = json.loads((scars_home / "plugins" / "claude" / "hooks" / "hooks.json").read_text())
    command = hooks["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]

    def fail(tool: str, udid: str) -> str:
        payload = {
            "session_id": "next-session",
            "cwd": str(repo),
            "hook_event_name": "PostToolUseFailure",
            "tool_name": tool,
            "tool_input": {"action": "screenshot", "device": udid},
            "tool_use_id": f"toolu_{tool}_{udid[:4]}",
            "error": SIM_ERROR.format(udid=udid),
        }
        result = subprocess.run(
            ["/bin/sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True, timeout=10
        )
        assert (result.returncode, result.stderr) == (0, "")
        return (
            json.loads(result.stdout)["hookSpecificOutput"].get("additionalContext", "")
            if result.stdout
            else ""
        )

    other = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
    assert fail(SIM_TOOL, other) == ""

    code, out, err = run(make_ctx(user_home, keys="a", clock=time.time), "inbox")
    assert (code, err) == (0, "")
    assert f'  avoid:   {SIM_TOOL} with device="{STALE}"\n' in out
    assert f'  instead: device="{LIVE}"\n' in out
    assert f'    failed: {{"action":"screenshot","device":"{STALE}"}}\n' in out
    assert f'    passed: device="{LIVE}"\n' in out
    db = store.connect(scars_home / "lessons.db")
    [lesson] = store.list_lessons(db)
    db.close()
    assert lesson.status.value == "promoted"

    context = fail(SIM_TOOL, other)
    assert context.splitlines()[1:4] == [
        f'AVOID: {SIM_TOOL} with device="{STALE}"',
        f'INSTEAD: device="{LIVE}"',
        "WHY: AVOID failed, INSTEAD passed",
    ]
    assert fail("mcp__other_simulator__control", other) == ""
