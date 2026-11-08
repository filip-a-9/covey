"""Real client processes for tests that need a register call from a known place.

The daemon runs inside pytest, so "who called" is only testable from subprocesses. These
speak raw HTTP over the unix socket with the stdlib; a symlinked `claude` interpreter may
not see the venv's site-packages.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import http.client, json, os, socket, subprocess, sys

class Conn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("covey")
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.path)

def register(sock, join, body):
    c = Conn(sock)
    c.request("POST", "/v1/agents/register", json.dumps(body),
              {"Authorization": "Bearer " + join, "Content-Type": "application/json"})
    r = c.getresponse()
    return {"status": r.status, "data": json.loads(r.read())}

sock, join, body, child, child_cwd, stay = sys.argv[1:7]
print(json.dumps(register(sock, join, json.loads(body))), flush=True)
if child != "-":
    # A process inside this one's tree registers `child` from `child_cwd`.
    subprocess.run([sys.executable, __file__, sock, join, child, "-", "-", "0"],
                   check=True, cwd=child_cwd)
if stay == "1":
    print("READY", flush=True)
    sys.stdin.readline()
"""


def _script(tmp: Path) -> Path:
    path = tmp / "register_client.py"
    path.write_text(SCRIPT)
    return path


def _argv(python, daemon, tmp, body, child=None, child_cwd=None, stay=False):
    return [
        str(python),
        str(_script(tmp)),
        daemon.socket_path,
        daemon.join_token,
        json.dumps(body),
        json.dumps(child) if child else "-",
        str(child_cwd) if child_cwd else ".",
        "1" if stay else "0",
    ]


def _results(out):
    assert out.returncode == 0, out.stderr
    return [
        (r["status"], r["data"])
        for r in (json.loads(x) for x in out.stdout.splitlines() if x.startswith("{"))
    ]


def register_from(daemon, body, cwd, tmp, python=sys.executable):
    """Register `body` from a fresh process whose cwd is `cwd`; (status, json). `python`
    may be a symlink named `claude`, so the daemon sees a session root."""
    out = subprocess.run(
        _argv(python, daemon, tmp, body),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (got,) = _results(out)
    return got


def register_inside(daemon, outer, inner, inner_cwd, tmp, fake_claude, outer_cwd=None):
    """`outer` registers from a fake `claude` (cwd `outer_cwd`), then `inner` from a child
    of it (cwd `inner_cwd`). Returns [(status, json), (status, json)]."""
    out = subprocess.run(
        _argv(fake_claude, daemon, tmp, outer, inner, inner_cwd),
        cwd=outer_cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return _results(out)
