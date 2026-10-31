import json
import os
import plistlib
import shlex
import time

import pytest

from cli_helpers import FAILURE_PAIR, Tty, claude_transcript, git_repo, make_ctx, run
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
    # The suite imports scars from this checkout's src/, which init refuses; stand in an installed copy.
    monkeypatch.setattr(install, "package_file", lambda: "/opt/scars lib/scars/__init__.py", raising=False)
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


def test_codex_merge_keeps_other_hooks_and_replaces_only_exact_scars_commands():
    guard = {"type": "command", "command": "python3 ~/guard.py"}
    mentions = {"type": "command", "command": "echo scars-hook is great"}
    existing = {
        "description": "mine",
        "hooks": {
            "Stop": [],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [guard]},
                {"matcher": "Edit", "hooks": []},
                {"hooks": [mentions]},
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"{QUOTED} codex PreToolUse"}, guard],
                },
                {"matcher": "Bash", "hooks": [{"type": "command", "command": f"{QUOTED} codex PreToolUse"}]},
            ],
        },
    }
    ours = install.hooks_config("codex/hooks.json", HOOK)
    merged = install.merge_codex(existing, ours)
    assert merged["description"] == "mine"
    assert merged["hooks"]["Stop"] == []
    assert merged["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [guard]},
        {"matcher": "Edit", "hooks": []},
        {"hooks": [mentions]},
        {"matcher": "Bash", "hooks": [guard]},
        *ours["hooks"]["PreToolUse"],
    ]
    assert list(merged["hooks"]) == ["Stop", "PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse"]
    assert install.merge_codex(merged, ours) == merged


@pytest.mark.parametrize(
    ("hooks", "detail"),
    [
        ([], "hooks is not an object"),
        ({"Stop": "echo hi"}, "hooks.Stop is not a list"),
        ({"Stop": ["echo hi"]}, "hooks.Stop[0] is not an object"),
        ({"Stop": [{"matcher": "x"}]}, "hooks.Stop[0].hooks is not a list"),
        ({"Stop": [{"hooks": {"command": "x"}}]}, "hooks.Stop[0].hooks is not a list"),
        ({"Stop": [{"hooks": ["echo hi"]}]}, "hooks.Stop[0].hooks[0] is not an object"),
    ],
)
def test_init_refuses_a_codex_config_of_another_shape_and_changes_nothing(machine, hooks, detail):
    user, scars_home = machine
    path = user / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"hooks": hooks, "other": 1})
    path.write_text(original)
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes")
    assert (code, out, err) == (
        1,
        "",
        f"scars init: {path} is not in the shape scars knows ({detail}); nothing was changed. "
        "Fix it or move it away, then run scars init again\n",
    )
    assert path.read_text() == original
    assert not (scars_home / "plugins").exists()
    assert not (scars_home / "bin").exists()


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


SHIM_BODY = "#!/bin/sh\nexec '/opt/scars bin/scars-hook' \"$@\"\n"


def mode(path):
    return oct(os.stat(path).st_mode & 0o777)


def test_init_writes_a_stable_shim_and_points_both_harnesses_at_it(machine):
    user, scars_home = machine
    assert run(make_ctx(user, clock=time.time), "init", "--yes")[0] == 0
    shim = scars_home / "bin" / "scars-hook"
    assert (shim.read_text(), mode(shim)) == (SHIM_BODY, "0o755")
    quoted = shlex.quote(str(shim))
    claude = json.loads((scars_home / "plugins" / "claude" / "hooks" / "hooks.json").read_text())
    codex = json.loads((user / ".codex" / "hooks.json").read_text())
    for config, harness in ((claude, "claude"), (codex, "codex")):
        commands = [h["command"] for e, gs in config["hooks"].items() for g in gs for h in g["hooks"]]
        events = list(config["hooks"])
        assert commands == [f"{quoted} {harness} {e}" for e in events]
    assert mode(user / ".codex" / "hooks.json") == "0o600"
    assert mode(scars_home / "plugins" / "claude" / "hooks" / "hooks.json") == "0o600"
    assert mode(user / "Library" / "LaunchAgents" / "dev.scars.miner.plist") == "0o644"


def test_init_refuses_a_hook_inside_a_git_checkout_unless_allowed(machine, work, monkeypatch):
    user, scars_home = machine
    checkout = work / "scars-src"
    (checkout / ".git").mkdir(parents=True)
    script = checkout / ".venv" / "bin" / "scars-hook"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install, "installed_script", lambda name: str(checkout / ".venv" / "bin" / name))
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes")
    assert (code, out, err) == (
        1,
        "",
        f"scars init: refused: {script} is inside the git checkout {checkout}, and an agent working in "
        "that checkout could edit the hook code every session runs; install scars outside a checkout, "
        "or pass --allow-dev-install to accept that\n",
    )
    assert not scars_home.exists()
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes", "--allow-dev-install")
    assert (code, err) == (0, "")
    assert f"warning: {script} is inside the git checkout {checkout}" in out
    assert (
        scars_home / "bin" / "scars-hook"
    ).read_text() == f'#!/bin/sh\nexec {shlex.quote(str(script))} "$@"\n'


def test_init_refuses_an_editable_install_whose_package_is_in_a_checkout(machine, work, monkeypatch):
    # `uv tool install -e .` puts scars-hook outside the checkout but imports scars from its src/.
    user, scars_home = machine
    checkout = work / "scars-src"
    (checkout / ".git").mkdir(parents=True)
    package = checkout / "src" / "scars"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    monkeypatch.setattr(install, "package_file", lambda: str(package / "__init__.py"), raising=False)
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes")
    assert (code, out, err) == (
        1,
        "",
        f"scars init: refused: {package} is inside the git checkout {checkout}, and an agent working in "
        "that checkout could edit the hook code every session runs; install scars outside a checkout, "
        "or pass --allow-dev-install to accept that\n",
    )
    assert not scars_home.exists()
    code, out, err = run(make_ctx(user, clock=time.time), "init", "--yes", "--allow-dev-install")
    assert (code, err) == (0, "")
    assert f"warning: {package} is inside the git checkout {checkout}" in out


def test_init_reads_and_writes_configs_as_utf8_whatever_the_locale(work):
    import subprocess
    import sys

    user = work / "user"
    scars_home = user / "sch\u00fctz"
    codex = user / ".codex" / "hooks.json"
    codex.parent.mkdir(parents=True)
    other = {"hooks": [{"type": "command", "command": "echo gr\u00fc\u00df"}]}
    codex.write_text(json.dumps({"hooks": {"Stop": [other]}}, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CODEX_SANDBOX", "SCARS_OFF")}
    env.update(HOME=str(user), SCARS_HOME=str(scars_home), LC_ALL="C", LANG="C")
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", PYTHONIOENCODING="utf-8")
    argv = [sys.executable, "-m", "scars", "init", "--yes", "--allow-dev-install"]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert (result.returncode, result.stderr) == (0, "")
    assert f"store: {scars_home}\n" in result.stdout
    merged = json.loads(codex.read_text(encoding="utf-8"))
    assert merged["hooks"]["Stop"][0] == other
    shim = shlex.quote(str(scars_home / "bin" / "scars-hook"))
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == f"{shim} codex PreToolUse"
    assert sorted(merged["hooks"]) == [
        "PostToolUse",
        "PreToolUse",
        "SessionStart",
        "Stop",
        "UserPromptSubmit",
    ]
    if sys.platform == "darwin":
        plist = user / "Library" / "LaunchAgents" / "dev.scars.miner.plist"
        assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"] == {"SCARS_HOME": str(scars_home)}


@pytest.mark.parametrize("variable", ["SCARS_HOME", "HOME"])
def test_init_refuses_a_home_that_is_not_utf8_without_a_traceback(work, variable):
    import subprocess
    import sys

    user = work / "user"
    user.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CODEX_SANDBOX", "SCARS_OFF")}
    env.update(HOME=str(user), SCARS_HOME=str(user / ".scars"))
    env[variable] = str(user / "sch\udcfctz")  # the byte 0xfc, which no UTF-8 text contains
    argv = [sys.executable, "-m", "scars", "init", "--yes", "--allow-dev-install"]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=60)
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        b"",
        f"scars init: {variable} is not valid UTF-8\n".encode(),
    )
    assert sorted(p.name for p in user.iterdir()) == []


def test_init_writes_utf8_configs_for_a_home_decoded_as_surrogate_escapes(machine, work, monkeypatch):
    """Under an ASCII locale Python decodes the UTF-8 bytes of `HOME` into surrogate escapes."""
    user = work / "j\u00fcrgen"
    escaped = str(work) + "/j\udcc3\udcbcrgen"
    assert os.fsencode(escaped) == os.fsencode(str(user))
    env = dict(os.environ, HOME=escaped, SCARS_HOME=escaped + "/sch\udcc3\udcbctz")
    monkeypatch.setattr(os, "environ", env)
    ctx = make_ctx(type(user)(escaped), clock=time.time)
    code, out, err = run(ctx, "init", "--yes")
    assert (code, err) == (0, "")
    scars_home = f"{user}/sch\u00fctz"
    assert f"store: {scars_home}\n" in out
    raw = (user / ".codex" / "hooks.json").read_bytes().decode("utf-8")
    command = json.loads(raw)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    shim = shlex.quote(f"{scars_home}/bin/scars-hook")
    assert command == f"{shim} codex PreToolUse"
    plugin = json.loads((user / "sch\u00fctz" / "plugins" / "claude" / "hooks" / "hooks.json").read_bytes())
    assert plugin["hooks"]["SessionStart"][0]["hooks"][0]["command"] == f"{shim} claude SessionStart"
    job = plistlib.loads((user / "Library" / "LaunchAgents" / "dev.scars.miner.plist").read_bytes())
    assert job["EnvironmentVariables"] == {"SCARS_HOME": scars_home}
    assert job["StandardOutPath"] == f"{scars_home}/logs/miner.log"


def test_init_output_cannot_crash_on_a_terminal_encoding_without_the_path(work):
    import subprocess
    import sys

    user = work / "user"
    user.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CODEX_SANDBOX", "SCARS_OFF")}
    env.update(HOME=str(user), SCARS_HOME=str(user / "sch\u00fctz"), PYTHONIOENCODING="ascii")
    argv = [sys.executable, "-m", "scars", "init", "--yes", "--allow-dev-install"]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=60)
    assert (result.returncode, result.stderr) == (0, b"")
    assert f"store: {user}/sch\\xfctz\n".encode() in result.stdout


def test_strict_json_keeps_numbers_that_survive_a_round_trip():
    loaded = install.load_json_exactly(
        '{"a": 0.1, "b": 3, "c": -2.5e-3, "d": 1e5, "e": 123456789012345678901234567890}'
    )
    assert loaded == {"a": 0.1, "b": 3, "c": -0.0025, "d": 100000.0, "e": 123456789012345678901234567890}


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ('{"hooks": {}, "other": 1, "other": 2}', 'duplicate key "other"'),
        ('{"hooks": {"Stop": [{"hooks": [], "x": 1, "x": 1}]}}', 'duplicate key "x"'),
        ('{"hooks": {}, "limit": 1e400}', "number 1e400 is not finite"),
        ('{"hooks": {}, "limit": NaN}', "number NaN is not finite"),
        ('{"hooks": {}, "limit": -Infinity}', "number -Infinity is not finite"),
        ('{"hooks": {}, "limit": 1e-400}', "number 1e-400 would change in a round trip"),
        (
            '{"hooks": {}, "limit": 1.00000000000000000001}',
            "number 1.00000000000000000001 would change in a round trip",
        ),
    ],
    ids=["duplicate", "nested-duplicate", "overflow", "nan", "infinity", "underflow", "precision"],
)
def test_init_refuses_json_that_would_not_survive_a_round_trip(machine, text, detail):
    user, scars_home = machine
    path = user / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert run(make_ctx(user, clock=time.time), "init", "--yes") == (
        1,
        "",
        f"scars init: {path} is not in the shape scars knows ({detail}); nothing was changed. "
        "Fix it or move it away, then run scars init again\n",
    )
    assert path.read_text() == text
    assert not scars_home.exists()


def test_init_does_not_overwrite_a_codex_config_edited_while_it_asked(machine, work):
    user, _ = machine
    transcript(user, git_repo(work / "lib", remote="https://example.com/x/lib.git"))
    path = user / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    edited = '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}'

    class EditsDuringPrompt(Tty):
        def readline(self, *args):
            if path.read_text() != edited:
                path.write_text(edited)
            return super().readline(*args)

    ctx = make_ctx(user, platform="linux", clock=time.time)
    ctx.stdin = EditsDuringPrompt("s\ny\ny\n")
    code, out, err = run(ctx, "init")
    assert (code, err) == (0, "")
    assert f"Codex hooks: not written: {path} changed since scars init read it; run scars init again\n" in out
    assert path.read_text() == edited
    assert "Claude Code plugin: written\n" in out
    assert run(make_ctx(user, lines="s\ny\ny\n", platform="linux", clock=time.time), "init")[0] == 0
    merged = json.loads(path.read_text())
    assert merged["hooks"]["Stop"] == json.loads(edited)["hooks"]["Stop"]
    assert "PreToolUse" in merged["hooks"]


def test_init_writes_through_a_symlinked_codex_config(machine, work):
    user, _ = machine
    real = work / "dotfiles" / "codex-hooks.json"
    real.parent.mkdir()
    real.write_text('{"other": 1}')
    link = user / ".codex" / "hooks.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert run(make_ctx(user, clock=time.time), "init", "--yes")[0] == 0
    assert link.is_symlink() and os.readlink(link) == str(real)
    merged = json.loads(real.read_text())
    assert (merged["other"], sorted(merged["hooks"])) == (
        1,
        ["PostToolUse", "PreToolUse", "SessionStart", "UserPromptSubmit"],
    )


def test_init_keeps_the_mode_of_an_existing_config(machine):
    user, _ = machine
    path = user / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0o640)
    assert run(make_ctx(user, clock=time.time), "init", "--yes")[0] == 0
    assert (mode(path), "PreToolUse" in json.loads(path.read_text())["hooks"]) == ("0o640", True)
    path.write_text("{}")
    path.chmod(0o600)
    assert run(make_ctx(user, clock=time.time), "init", "--yes")[0] == 0
    assert (mode(path), "PreToolUse" in json.loads(path.read_text())["hooks"]) == ("0o600", True)


def test_init_leaves_a_read_only_config_alone_unless_confirmed(machine):
    user, _ = machine
    path = user / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0o444)
    out = run(make_ctx(user, platform="linux", clock=time.time), "init", "--yes")[1]
    assert (
        f"Codex hooks: not written: {path} is read-only; run scars init without --yes to replace it\n" in out
    )
    assert path.read_text() == "{}"
    out = run(make_ctx(user, lines="y\nn\n", platform="linux", clock=time.time), "init")[1]
    assert f"write the Codex hooks? [y/N] {path} is read-only; replace it anyway? [y/N] " in out
    assert "Codex hooks: not written\n" in out
    assert path.read_text() == "{}"
    out = run(make_ctx(user, lines="y\ny\n", platform="linux", clock=time.time), "init")[1]
    assert "Codex hooks: written\n" in out
    assert (mode(path), "PreToolUse" in json.loads(path.read_text())["hooks"]) == ("0o444", True)


def test_concurrent_writes_of_one_file_never_fail_or_leave_temp_files(tmp_path):
    import threading

    target = tmp_path / "codex" / "hooks.json"
    target.parent.mkdir()
    errors = []

    def writer(n):
        for i in range(150):
            try:
                install.PlannedFile(target, f"{n}-{i}\n").write()
            except Exception as exc:
                errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [p.name for p in target.parent.iterdir()] == ["hooks.json"]
    assert target.read_text().endswith("-149\n")


def test_two_inits_at_once_both_succeed(work):
    import subprocess
    import sys

    user = work / "home with space"
    user.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("SCARS_HOME", "CLAUDECODE", "CODEX_SANDBOX")}
    env["HOME"] = str(user)
    argv = [sys.executable, "-m", "scars", "init", "--yes", "--allow-dev-install"]
    pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    procs = [subprocess.Popen(argv, env=env, **pipes) for _ in range(2)]
    outputs = [p.communicate(timeout=60) for p in procs]
    results = [(p.returncode, err) for p, (_, err) in zip(procs, outputs, strict=True)]
    assert results == [(0, ""), (0, "")]
    codex = json.loads((user / ".codex" / "hooks.json").read_text())
    assert sorted(codex["hooks"]) == ["PostToolUse", "PreToolUse", "SessionStart", "UserPromptSubmit"]
    assert [len(groups) for groups in codex["hooks"].values()] == [1, 1, 1, 1]
    leftovers = [p for p in user.rglob("*") if "scars-tmp" in p.name]
    assert leftovers == []


def test_init_yes_never_registers_a_repo_whose_remotes_cannot_be_read(machine, work, monkeypatch):
    user, scars_home = machine
    repo = git_repo(work / "app")
    transcript(user, repo)
    monkeypatch.setattr(install, "_git", lambda *args: None)
    out = run(make_ctx(user, clock=time.time), "init", "--yes")[1]
    assert f"repo app ({repo}/.git): cannot tell whether it has a git remote; skipped\n" in out
    db = store.connect(scars_home / "lessons.db")
    assert db.execute("select count(*) from repos").fetchone()[0] == 0


def test_store_directory_is_private(machine):
    user, scars_home = machine
    run(make_ctx(user, platform="linux", clock=time.time), "init", "--yes")
    assert oct(os.stat(scars_home).st_mode & 0o777) == "0o700"
    assert oct(os.stat(scars_home / "lessons.db").st_mode & 0o777) == "0o600"
