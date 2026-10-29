import os
import subprocess
import sys


def run_hook(tmp_path, stdin, **env):
    return subprocess.run(
        [sys.executable, "-m", "scars", "hook", "claude", "pre_tool"],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "SCARS_HOME": str(tmp_path), "SCARS_DEBUG": "1", **env},
        timeout=10,
    )


def test_hook_failure_exits_zero_with_empty_output_and_logs_the_cause(tmp_path):
    result = run_hook(tmp_path, "not json")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert "NotImplementedError" in (tmp_path / "logs" / "hook.log").read_text()


def test_kill_switch_file_skips_all_work(tmp_path):
    (tmp_path / "OFF").touch()
    result = run_hook(tmp_path, "not json")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / "logs").exists()


def test_kill_switch_env_var_skips_all_work(tmp_path):
    result = run_hook(tmp_path, "not json", SCARS_OFF="1")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / "logs").exists()
