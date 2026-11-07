"""`python -m covey` runs the same app as the `covey` command."""

import subprocess
import sys


def test_python_m_covey_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "covey", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "help printed nothing"
