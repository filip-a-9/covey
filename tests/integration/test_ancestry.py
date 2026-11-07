"""The human token is refused from inside a registered agent's process tree.

The daemon runs in this pytest process, so agents must be real subprocesses. The fake
`claude` is this interpreter symlinked as `claude`, so its argv0 is `claude`. It and its
children speak raw HTTP over the unix socket with the stdlib (a symlinked interpreter may
not see the venv's site-packages).
"""

import contextlib
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

CLIENT = r"""
import http.client, json, socket, subprocess, sys

class Conn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("covey")
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.path)

def call(path, method, url, token, body=None):
    c = Conn(path)
    c.request(method, url, json.dumps(body) if body else None,
              {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())

def human_call(sock, token):
    status, data = call(sock, "GET", "/v1/approvals", token)
    print(json.dumps({"status": status, "data": data}), flush=True)

mode, sock, human, join, slug = sys.argv[1:6]
if mode == "human":
    human_call(sock, human)
elif mode == "agent":
    # Register as a session, then run a child that tries the human token.
    status, _ = call(sock, "POST", "/v1/agents/register", join,
        {"slug": slug, "kind": "python", "delivery": "poll", "summary": "fake session"})
    assert status == 200, status
    subprocess.run([sys.executable, __file__, "human", *sys.argv[2:]], check=True)
    if len(sys.argv) > 6:  # stay alive so the test can probe from outside the tree
        print("READY", flush=True)
        sys.stdin.readline()
"""


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "client.py"
    path.write_text(CLIENT)
    return path


@pytest.fixture
def fake_claude(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "claude"
    link.symlink_to(sys.executable)
    return link


def run(argv, daemon, slug="x"):
    out = subprocess.run(
        [*argv, daemon.socket_path, daemon.human_token, daemon.join_token, slug],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return [
        json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")
    ]


def test_child_of_a_registered_session_is_refused(daemon, client, fake_claude):
    # Skip the interpreter's venv lookup quirks: only argv0 matters to the daemon.
    (result,) = run([str(fake_claude), str(client), "agent"], daemon, "alice")
    assert result["status"] == 403
    error = result["data"]["error"]
    assert error["code"] == "human_only"
    assert "alice" in error["message"]
    assert "terminal" in error["next_step"]


def test_sibling_of_a_session_is_accepted(daemon, client, fake_claude):
    proc = subprocess.Popen(
        [
            str(fake_claude),
            str(client),
            "agent",
            daemon.socket_path,
            daemon.human_token,
            daemon.join_token,
            "alice",
            "stay",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        lines = []
        while (line := proc.stdout.readline().strip()) != "READY":
            assert line, "fake claude died"
            lines.append(line)
        assert json.loads(lines[0])["status"] == 403  # its own child is refused
        (result,) = run([sys.executable, str(client), "human"], daemon)
        assert result["status"] == 200
    finally:
        proc.stdin.close()
        proc.wait(10)


def test_session_that_ended_is_no_longer_checked(daemon, client, fake_claude):
    # After the fake claude exits its pid is gone; a new process is never matched by it.
    (first,) = run([str(fake_claude), str(client), "agent"], daemon, "alice")
    assert first["status"] == 403
    time.sleep(0.1)
    (result,) = run([sys.executable, str(client), "human"], daemon)
    assert result["status"] == 200


def test_fake_claude_is_recognised_by_name(fake_claude, tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os\n"
        "from covey.procs import find_claude\n"
        "print(json.dumps(find_claude(os.getpid())))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    out = subprocess.run(
        [str(fake_claude), str(probe)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["pid"] > 1


def test_human_token_is_refused_when_the_peer_pid_is_unreadable(daemon, monkeypatch):
    from covey.daemon import peer

    monkeypatch.setattr(peer, "peer_pid", lambda sock: None)
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path), base_url="http://covey"
    ) as c:
        r = c.get(
            "/v1/approvals", headers={"Authorization": f"Bearer {daemon.human_token}"}
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "human_only"
    assert "doctor" in r.json()["error"]["next_step"]


SHIM_SESSION = r"""
import subprocess, sys
python, client, home = sys.argv[1:4]
# Like the real shim: a long-lived child of `claude` that starts coveyd if none answers,
# then registers. The human call comes from a sibling, as a Bash tool call would.
START = (
    "import httpx; from covey import config; from covey.launch import ensure_daemon\n"
    "cfg = config.load(); ensure_daemon(cfg)\n"
    "c = httpx.Client(transport=httpx.HTTPTransport(uds=str(cfg.socket_path)), base_url='http://c')\n"
    "r = c.post('/v1/agents/register', json={'slug': 'alice', 'kind': 'python', 'delivery': 'poll'},\n"
    "           headers={'Authorization': 'Bearer ' + cfg.join_token_path.read_text().strip()})\n"
    "assert r.status_code == 200, r.text\n"
    "print('UP', flush=True); input()\n"
)
if len(sys.argv) > 4:  # coveyd started straight from the session, as `coveyd &` would
    daemon = subprocess.Popen([python, "-m", "covey.daemon"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket, time
    for _ in range(100):
        try:
            socket.socket(socket.AF_UNIX).connect(f"{home}/covey.sock")
            break
        except OSError:
            time.sleep(0.1)
shim = subprocess.Popen([python, "-c", START], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True)
assert shim.stdout.readline().strip() == "UP"
read = lambda name: open(f"{home}/{name}").read().strip()
subprocess.run([sys.executable, client, "human", f"{home}/covey.sock",
                read("token"), read("join-token"), "alice"], check=True)
shim.stdin.close()
shim.wait(10)
if len(sys.argv) > 4:
    daemon.kill()
"""


@pytest.mark.parametrize("started_by", ["shim", "session"])
def test_a_session_that_started_the_daemon_is_still_refused(
    started_by, short_home, monkeypatch, client, fake_claude, tmp_path
):
    from tests.integration.conftest import free_port

    home = short_home / "h"
    monkeypatch.setenv("COVEY_HOME", str(home))
    monkeypatch.setenv("COVEY_PORT", str(free_port()))
    script = tmp_path / "session.py"
    script.write_text(SHIM_SESSION)
    try:
        out = subprocess.run(
            [str(fake_claude), str(script), sys.executable, str(client), str(home)]
            + (["direct"] if started_by == "session" else []),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        (result,) = [
            json.loads(x) for x in out.stdout.splitlines() if x.startswith("{")
        ]
        assert result["status"] == 403
        assert result["data"]["error"]["code"] == "human_only"
    finally:
        pid_file = home / "coveyd.pid"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), 9)
