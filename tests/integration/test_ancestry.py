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

from covey.config import Config
from tests.integration.conftest import NO_CLAUDE, free_port, run_daemon

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
elif mode == "session":
    # Already registered elsewhere: only replace the session id, as SessionStart does
    # after /clear. `join` carries the agent token here.
    status, _ = call(sock, "PUT", "/v1/agents/" + slug + "/session", join,
        {"session_id": "after-clear"})
    assert status == 200, status
    subprocess.run([sys.executable, __file__, "human", *sys.argv[2:]], check=True)
if mode in ("agent", "session") and len(sys.argv) > 6:
    # stay alive so the test can probe from outside the tree
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


def test_replacing_the_session_id_records_the_session_root(daemon, client, fake_claude):
    # Registered over TCP, so no root is known until the PUT /session from inside.
    with httpx.Client(base_url=daemon.base_url, timeout=15) as tcp:
        r = tcp.post(
            "/v1/agents/register",
            json={"slug": "alice", "kind": "claude-code", "delivery": "hooks"},
            headers={"Authorization": f"Bearer {daemon.join_token}"},
        )
    assert r.status_code == 200, r.text
    out = subprocess.run(
        [
            str(fake_claude),
            str(client),
            "session",
            daemon.socket_path,
            daemon.human_token,
            r.json()["agent_token"],
            "alice",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    (result,) = [json.loads(x) for x in out.stdout.splitlines() if x.startswith("{")]
    assert result["status"] == 403
    assert result["data"]["error"]["code"] == "human_only"


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


# --- S2 ancestry hardening: one test per escape ---------------------------------------

ROOT = r"""
import http.client, json, socket, subprocess, sys
sock, human, join, slug = sys.argv[1:5]

def call(method, url, token, body=None):
    c = http.client.HTTPConnection("covey")
    c.sock = socket.socket(socket.AF_UNIX)
    c.sock.connect(sock)
    c.request(method, url, json.dumps(body) if body is not None else None,
              {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, r.read().decode()

PROBE = '''
import http.client, socket, sys
c = http.client.HTTPConnection("covey")
c.sock = socket.socket(socket.AF_UNIX)
c.sock.connect(sys.argv[1])
c.request("GET", "/v1/approvals", headers={"Authorization": "Bearer " + sys.argv[2]})
print(c.getresponse().status, flush=True)
'''
FIRE = '''
import os, socket, sys, json
body = json.dumps({"requester": "alice", "target": "bob", "capability": sys.argv[3],
                   "scope": "always"})
req = ("POST /v1/grants HTTP/1.1\\r\\nHost: covey\\r\\nAuthorization: Bearer %s\\r\\n"
       "Content-Type: application/json\\r\\nContent-Length: %d\\r\\nConnection: close\\r\\n\\r\\n%s"
       % (sys.argv[2], len(body), body)).encode()
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(req)
os._exit(0)
'''
REG = '''
import http.client, json, socket, sys
c = http.client.HTTPConnection("covey")
c.sock = socket.socket(socket.AF_UNIX)
c.sock.connect(sys.argv[1])
c.request("POST", "/v1/agents/register", json.dumps({"slug": sys.argv[3], "kind": "python",
          "delivery": "poll"}), {"Authorization": "Bearer " + sys.argv[2],
          "Content-Type": "application/json"})
print(c.getresponse().read().decode())
'''
if len(sys.argv) > 5:  # shim mode: a long-lived child registers, the root only probes
    out = subprocess.run([sys.executable, "-c", REG, sock, join, slug],
                         capture_output=True, text=True)
    token = json.loads(out.stdout)["agent_token"]
    print(json.dumps({"registered": 200, "token": token}), flush=True)
else:
  for slug_ in (slug,):
    st, body = call("POST", "/v1/agents/register", join,
                    {"slug": slug_, "kind": "python", "delivery": "poll"})
    if slug_ == slug:
        token = json.loads(body)["agent_token"]
  print(json.dumps({"registered": st, "token": token}), flush=True)
for line in sys.stdin:
    cmd = line.split()
    if cmd[0] == "probe":
        out = subprocess.run([sys.executable, "-c", PROBE, sock, human],
                             capture_output=True, text=True)
        print(out.stdout.strip(), flush=True)
    elif cmd[0] == "fire":
        subprocess.run([sys.executable, "-c", FIRE, sock, human, cmd[1]]).check_returncode()
        print("sent", flush=True)
"""


class Root:
    """A fake session process named `name`, driven over stdin."""

    def __init__(self, tmp_path, daemon, name="claude", slug="alice", via_shim=False):
        link = tmp_path / "bin" / name
        link.parent.mkdir(exist_ok=True)
        if not link.exists():
            link.symlink_to(sys.executable)
        script = tmp_path / "root.py"
        script.write_text(ROOT)
        self.proc = subprocess.Popen(
            [
                str(link),
                str(script),
                daemon.socket_path,
                daemon.human_token,
                daemon.join_token,
                slug,
                *(["shim"] if via_shim else []),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.token = json.loads(self.proc.stdout.readline())["token"]

    def cmd(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(10)


def human_client(daemon):
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers={"Authorization": f"Bearer {daemon.human_token}"},
    )


BEAT = """
import http.client, socket, sys
c = http.client.HTTPConnection("covey")
c.sock = socket.socket(socket.AF_UNIX)
c.sock.connect(sys.argv[1])
c.request("POST", "/v1/agents/" + sys.argv[3] + "/heartbeat",
          headers={"Authorization": "Bearer " + sys.argv[2]})
print(c.getresponse().status)
"""


def outside_beat(daemon, slug, token):
    """A heartbeat from a process of its own, outside every agent's tree."""
    out = subprocess.run(
        [sys.executable, "-c", BEAT, daemon.socket_path, token, slug],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout)


def test_f1_a_child_that_exits_at_once_does_not_get_a_grant(daemon, tmp_path):
    with httpx.Client(base_url=daemon.base_url) as tcp:  # the grant's target, no root
        tcp.post(
            "/v1/agents/register",
            json={"slug": "bob", "kind": "python", "delivery": "poll"},
            headers={"Authorization": f"Bearer {daemon.join_token}"},
        ).raise_for_status()
    root = Root(tmp_path, daemon)
    try:
        for i in range(20):
            assert root.cmd(f"fire cap{i}") == "sent"
        time.sleep(0.5)
        with human_client(daemon) as c:
            grants = c.get("/v1/grants").json()
        assert not grants.get("grants", grants)
    finally:
        root.close()


def test_f8_a_heartbeat_from_outside_does_not_replace_the_session_root(
    daemon, tmp_path
):
    root = Root(tmp_path, daemon)
    try:
        assert root.cmd("probe") == "403"
        outside_beat(daemon, "alice", root.token)  # whatever its status, no new root
        assert root.cmd("probe") == "403"
    finally:
        root.close()


def test_f10_roots_survive_a_daemon_restart(short_home, tmp_path):
    from covey.mcp.identity import write_session_file
    from covey.procs import _start_time

    cfg = Config(
        home=short_home / "h",
        port=free_port(),
        sweep_interval_s=0.2,
        claude_bin=NO_CLAUDE,
    )
    with run_daemon(cfg) as d:
        root = Root(tmp_path, d)
        # what the real shim does after registering
        pid = root.proc.pid
        write_session_file(
            cfg, {"pid": pid, "start_time": _start_time(pid)}, "alice", root.token
        )
    try:
        with run_daemon(cfg):
            assert root.cmd("probe") == "403"  # no heartbeat since the restart
    finally:
        root.close()


def test_f11_a_session_without_a_claude_ancestor_is_still_checked(daemon, tmp_path):
    root = Root(tmp_path, daemon, name="node", via_shim=True)
    try:
        assert root.cmd("probe") == "403"
    finally:
        root.close()
