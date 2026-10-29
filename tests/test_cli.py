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


def test_unbuilt_command_exits_2_with_a_message():
    result = scars("inbox")
    assert (result.returncode, result.stderr.strip()) == (2, "scars inbox: not implemented yet")
