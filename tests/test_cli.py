import subprocess
import sys


def scars(*args):
    return subprocess.run([sys.executable, "-m", "scars", *args], capture_output=True, text=True)


def test_version_flag_prints_package_version():
    result = scars("--version")
    assert (result.returncode, result.stdout.strip()) == (0, "scars 0.0.1")


def test_help_lists_phase_1_commands():
    result = scars("--help")
    assert result.returncode == 0
    for name in ("init", "inbox", "why", "doctor", "off", "hook"):
        assert f"    {name}" in result.stdout


def test_unbuilt_command_exits_2_with_a_message(monkeypatch, capsys):
    from scars import cli

    monkeypatch.setattr(cli, "PHASE_1_COMMANDS", (*cli.PHASE_1_COMMANDS, cli.Command("later", "not built")))
    assert cli.main(["later"]) == 2
    assert capsys.readouterr().err == "scars later: not implemented yet\n"


def test_every_phase_1_command_has_a_handler():
    from scars import cli

    assert [c.name for c in cli.PHASE_1_COMMANDS if c.handler is None] == []


def test_privileged_command_run_from_a_pipe_is_refused(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "scars", "off"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "SCARS_HOME": str(tmp_path)},
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "",
        "scars off: refused: stdin and stdout must be a terminal; run it in your own terminal\n",
    )
    assert not (tmp_path / "OFF").exists()
