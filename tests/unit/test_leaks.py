"""The leak guard in tests/leaks.py finds a demo coveyd by its argv and COVEY_HOME."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests import leaks

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the guard reads /proc"
)


def fake_daemon(home: Path) -> subprocess.Popen:
    """A sleeping process whose argv says covey.daemon, with COVEY_HOME set to `home`."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "-m", "covey.daemon"],
        env={**os.environ, "COVEY_HOME": str(home), "COVEY_PORT": "1"},
    )


def wait_until_found(pid: int) -> bool:
    """The child's argv is only set once it has exec'd, so poll briefly."""
    import time

    for _ in range(50):
        if pid in leaks.demo_daemons():
            return True
        time.sleep(0.05)
    return False


def test_a_demo_daemon_is_found_described_and_stopped():
    home = Path(tempfile.mkdtemp(prefix="covey-demo-", dir="/tmp"))
    (home / "coveyd.log").write_text("first line\nthe last thing it said\n")
    proc = fake_daemon(home)
    try:
        assert wait_until_found(proc.pid)
        env = leaks.demo_daemons()[proc.pid]
        assert env["COVEY_HOME"] == str(home)

        report = leaks.describe(proc.pid, env)
        assert str(proc.pid) in report
        assert "the last thing it said" in report

        leaks.stop(proc.pid)
        assert proc.wait(timeout=1) is not None
        assert proc.pid not in leaks.demo_daemons()
    finally:
        proc.kill()
        proc.wait()
        (home / "coveyd.log").unlink(missing_ok=True)
        home.rmdir()


def test_a_daemon_with_a_home_outside_the_demo_prefix_is_not_found(tmp_path):
    proc = fake_daemon(tmp_path / "home")
    try:
        # Give it the same time the positive test needs to show up.
        assert not wait_until_found(proc.pid)
    finally:
        proc.kill()
        proc.wait()
