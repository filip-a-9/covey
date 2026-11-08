"""Find, describe and stop demo coveyd processes that outlive the test run. Linux only: it
reads /proc, which is where the leak was seen (.claude/specs/test-daemon-leak.md)."""

import os
import signal
import subprocess
import time
from pathlib import Path

from covey.config import Config

DEMO_PREFIX = "/tmp/covey-demo-"


def _read(pid: int, name: str) -> list[str]:
    return Path(f"/proc/{pid}/{name}").read_bytes().decode(errors="replace").split("\0")


def demo_daemons() -> dict[int, dict[str, str]]:
    """Every live process running covey.daemon with a COVEY_HOME under /tmp/covey-demo-,
    mapped to its COVEY_* environment."""
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if "covey.daemon" not in _read(pid, "cmdline"):
                continue
            env = dict(
                item.split("=", 1)
                for item in _read(pid, "environ")
                if item.startswith("COVEY_") and "=" in item
            )
        except OSError:  # gone, or not ours to read
            continue
        if env.get("COVEY_HOME", "").startswith(DEMO_PREFIX):
            found[pid] = env
    return found


def describe(pid: int, env: dict[str, str]) -> str:
    """What is known about one leaked daemon, for the failure report."""
    ps = subprocess.run(
        ["ps", "-o", "ppid=,lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    home = Path(env.get("COVEY_HOME", ""))
    lines = [
        f"pid {pid}  ppid+start: {ps or '(gone)'}",
        f"COVEY_HOME={home}  exists: {home.exists()}",
        f"COVEY_PORT={env.get('COVEY_PORT', '(unset)')}",
    ]
    log = Config(home=home, port=0).log_path
    if log.exists():
        tail = log.read_text(errors="replace").splitlines()[-20:]
        lines.append(f"last {len(tail)} lines of {log}:")
        lines += [f"  {line}" for line in tail]
    else:
        lines.append(f"no log at {log}")
    return "\n".join(lines)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"  # a zombie has exited; only its parent's wait() is missing


def stop(pid: int) -> None:
    """SIGTERM, up to 5 s to exit, then SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
