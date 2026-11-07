"""Process lookups shared by the shim and the daemon. Stdlib only: the shim starts on every
Claude Code session, so nothing heavy may be imported here."""

import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict


class ClaudeProcess(TypedDict):
    pid: int
    start_time: str


def _parent_and_argv0(pid: int) -> tuple[int, str] | None:
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        # The command name in field 2 may contain spaces; fields after the last ")" do not.
        return int(stat.rsplit(")", 1)[1].split()[1]), argv[0].decode(errors="replace")
    out = _ps(pid, "ppid=,comm=")
    if out is None:
        return None
    ppid, _, comm = out.partition(" ")
    return int(ppid), comm.strip()


def _start_time(pid: int) -> str | None:
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        return stat.rsplit(")", 1)[1].split()[19]  # field 22, starttime in clock ticks
    return _ps(pid, "lstart=")


def _ps(pid: int, fields: str) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", fields, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def find_claude(pid: int) -> ClaudeProcess | None:
    """The nearest process named `claude` at or above `pid`, or None."""
    seen = 0
    while pid > 1 and seen < 32:
        found = _parent_and_argv0(pid)
        if found is None:
            return None
        parent, argv0 = found
        if os.path.basename(argv0) == "claude":
            start = _start_time(pid)
            return None if start is None else {"pid": pid, "start_time": start}
        pid, seen = parent, seen + 1
    return None
