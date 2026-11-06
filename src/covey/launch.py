"""Start coveyd in the background. Shared by `covey up` and the MCP shim, so no Typer here.

Liveness is a token-free probe: any Covey answer on the socket counts. The human token is
not used, because the daemon refuses it from inside an agent's process tree (SPEC.md,
"Trust model"), and the shim runs there.
"""

import os
import subprocess
import sys
import time

import httpx

from covey.config import Config


class LaunchError(Exception):
    """coveyd did not come up. The message says why and what to do next."""


def read_pid(cfg: Config) -> int | None:
    try:
        return int(cfg.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our own exited child: reap it
            return False
    except ChildProcessError:
        pass  # not our child, the normal case across two `covey` runs
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def last_log_lines(cfg: Config, n: int = 15) -> str:
    try:
        return "\n".join(cfg.log_path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log yet)"


def daemon_answers(cfg: Config) -> bool:
    if not cfg.socket_path.exists():
        return False
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(cfg.socket_path)),
            base_url="http://covey",
            timeout=2.0,
        ) as c:
            body = c.get("/v1/whoami").json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(body, dict) and "covey" in body


def _answers_within(cfg: Config, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if daemon_answers(cfg):
            return True
        time.sleep(0.1)
    return False


def ensure_daemon(cfg: Config, timeout_s: float = 10.0) -> None:
    """Return once coveyd answers on the socket, starting it if needed."""
    if daemon_answers(cfg):
        return
    cfg.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(cfg.log_path, "ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "covey.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if child.poll() is not None:
            # Another shim may have started coveyd at the same moment and won the bind.
            if _answers_within(cfg, 2.0):
                return
            raise LaunchError(
                f"coveyd exited with code {child.returncode}. Last log lines:\n"
                f"{last_log_lines(cfg)}\nFix the problem above, then run `covey up` again. "
                f"Full log: {cfg.log_path}"
            )
        if daemon_answers(cfg):
            return
        time.sleep(0.1)
    raise LaunchError(
        f"coveyd did not answer within {timeout_s:g} s. Check the log at {cfg.log_path}, "
        "then run `covey down` and `covey up` again."
    )
