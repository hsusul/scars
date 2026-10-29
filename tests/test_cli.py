import subprocess
import sys


def test_version_flag_prints_package_version():
    result = subprocess.run([sys.executable, "-m", "scars", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "scars 0.0.1"
