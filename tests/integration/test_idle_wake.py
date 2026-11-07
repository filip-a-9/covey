"""Idle wake end to end: a real daemon posts to a fake Claude Code session's messaging socket.

SAFETY: the suite may run inside a live Claude Code session. The socket path is only ever the
fake's, passed explicitly; nothing here reads it from the environment.
"""

import json
import os
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest

from covey.config import Config
from covey.frames import CALL_TO_ACTION, FRAME
from covey.mcp.identity import read_wake_file
from covey.procs import _start_time
from tests.integration.conftest import NO_CLAUDE, free_port, run_daemon
from tests.integration.test_hooks_api import auth

WINDOW_S = 0.5
SETTLE_S = 0.3
TASK = "SECRET-TASK-TEXT-do-not-leak"


class FakeSession:
    """A unix-socket server standing in for a Claude Code session. Each connection's lines
    are recorded as parsed JSON."""

    def __init__(self, path):
        self.path = str(path)
        self.messages: list[list[dict]] = []
        self._server = socket.socket(socket.AF_UNIX)
        self._server.bind(self.path)
        self._server.listen(8)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while chunk := conn.recv(4096):
                        data += chunk
                except OSError:
                    pass
            lines = [json.loads(x) for x in data.decode().splitlines() if x]
            self.messages.append(lines)

    def wait_for(self, n, timeout=3.0):
        end = time.monotonic() + timeout
        while len(self.messages) < n and time.monotonic() < end:
            time.sleep(0.02)
        return self.messages

    def close(self):
        self._stop = True
        self._server.close()


@pytest.fixture
def fake(short_home):
    session = FakeSession(short_home / "s.sock")
    yield session
    session.close()


def make_daemon(short_home, **overrides):
    cfg = Config(
        home=short_home / "h",
        port=free_port(),
        sweep_interval_s=0.2,
        claude_bin=NO_CLAUDE,
        idle_wake_fallback_s=WINDOW_S,
    )
    return run_daemon(replace(cfg, **overrides))


@pytest.fixture
def wake_daemon(short_home):
    with make_daemon(short_home) as d:
        yield d


@pytest.fixture
def tcp(wake_daemon):
    with httpx.Client(base_url=wake_daemon.base_url, timeout=15) as c:
        yield c


@pytest.fixture
def human(wake_daemon):
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=wake_daemon.socket_path),
        base_url="http://covey",
        headers=auth(wake_daemon.human_token),
        timeout=15,
    ) as c:
        yield c


def register(tcp, daemon, slug, wake_socket=None, wakeable=False):
    if wakeable:  # only the human token can set it: pre-add the agent, then register
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=daemon.socket_path),
            base_url="http://covey",
            headers=auth(daemon.human_token),
        ) as human:
            made = human.post(
                "/v1/agents", json={"slug": slug, "workdir": "/x", "wakeable": True}
            )
            assert made.status_code == 201, made.text
    body = {
        "slug": slug,
        "kind": "claude-code",
        "delivery": "hooks",
        "session_id": "s1",
        "capabilities": [
            {"name": "explain", "level": "open"},
            {"name": "run_tests", "level": "gated"},
        ],
    }
    if wake_socket:
        body["wake_socket"] = wake_socket
    r = tcp.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
    assert r.status_code == 200, r.text
    return auth(r.json()["agent_token"])


def heartbeat(tcp, headers, wake_socket):
    r = tcp.post(
        "/v1/agents/api/heartbeat", json={"wake_socket": wake_socket}, headers=headers
    )
    assert r.status_code == 200, r.text


def ask(tcp, web, capability="explain"):
    r = tcp.post(
        "/v1/requests",
        json={"to": "api", "capability": capability, "task": TASK},
        headers=web,
    )
    assert r.status_code == 201, r.text
    return r.json()["request"]


def stop_claim(tcp, api):
    r = tcp.post("/v1/agents/api/inbox/stop-claim", headers=api)
    assert r.status_code == 200, r.text
    return r.json()["requests"]


def status(tcp, web, rid):
    return tcp.get(f"/v1/requests/{rid}", headers=web).json()["request"]["status"]


def poll_stop_claim(tcp, api, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        claimed = stop_claim(tcp, api)
        if claimed:
            return claimed
        time.sleep(0.05)
    return []


def assert_wake_message(lines, rid):
    assert len(lines) == 2
    assert lines[0]["type"] == "auth"
    assert lines[1]["type"] == "user"
    content = lines[1]["message"]["content"]
    assert FRAME in content and CALL_TO_ACTION in content
    assert "web" in content and rid in content
    assert TASK not in content


def test_a_live_agent_is_woken_and_a_read_acks_it(tcp, wake_daemon, fake):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api", fake.path)
    req = ask(tcp, web)

    messages = fake.wait_for(1)
    assert len(messages) == 1
    assert_wake_message(messages[0], req["request_id"])
    assert status(tcp, web, req["request_id"]) == "queued"
    assert stop_claim(tcp, api) == []

    inbox = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
    assert [r["request_id"] for r in inbox] == [req["request_id"]]
    assert status(tcp, web, req["request_id"]) == "delivered"
    time.sleep(WINDOW_S + 0.6)
    assert status(tcp, web, req["request_id"]) == "delivered"
    assert len(fake.messages) == 1


def test_an_unread_wake_falls_back_to_the_stop_hook_without_a_second_post(
    tcp, wake_daemon, fake
):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api", fake.path)
    req = ask(tcp, web)
    fake.wait_for(1)
    assert stop_claim(tcp, api) == []

    claimed = poll_stop_claim(tcp, api)
    assert [r["request_id"] for r in claimed] == [req["request_id"]]
    time.sleep(SETTLE_S)
    assert len(fake.messages) == 1


def test_a_connect_error_releases_at_once_and_drops_the_socket(short_home, fake):
    dead = short_home / "dead.sock"
    with (
        make_daemon(short_home, idle_wake_fallback_s=30.0) as d,
        httpx.Client(base_url=d.base_url, timeout=15) as tcp,
    ):
        web = register(tcp, d, "web")
        api = register(tcp, d, "api", str(dead))
        first = ask(tcp, web)
        claimed = poll_stop_claim(tcp, api, timeout=3.0)
        assert [r["request_id"] for r in claimed] == [first["request_id"]]

        # A listener now appears at the dropped path: nothing is posted to it.
        live = FakeSession(dead)
        try:
            ask(tcp, web)
            time.sleep(SETTLE_S)
            assert live.messages == []
            heartbeat(tcp, api, str(dead))
            third = ask(tcp, web)
            messages = live.wait_for(1)
            assert len(messages) == 1
            assert_wake_message(messages[0], third["request_id"])
        finally:
            live.close()
    assert fake.messages == []


def test_a_heartbeat_can_arm_the_wake_socket(tcp, wake_daemon, fake):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api")
    heartbeat(tcp, api, fake.path)
    req = ask(tcp, web)
    messages = fake.wait_for(1)
    assert len(messages) == 1
    assert_wake_message(messages[0], req["request_id"])


def test_a_session_end_stops_the_posts(tcp, wake_daemon, fake):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api", fake.path, wakeable=True)
    r = tcp.request(
        "DELETE", "/v1/agents/api/session", json={"reason": "logout"}, headers=api
    )
    assert r.status_code == 200, r.text
    ask(tcp, web)
    # The session comes back without a socket of its own: the old one must be forgotten.
    r = tcp.put("/v1/agents/api/session", json={"session_id": "s2"}, headers=api)
    assert r.status_code == 200, r.text
    assert r.json()["liveness"] == "live"
    ask(tcp, web)
    time.sleep(SETTLE_S)
    assert fake.messages == []


def test_an_approved_request_is_posted_once_approved(tcp, human, wake_daemon, fake):
    web = register(tcp, wake_daemon, "web")
    register(tcp, wake_daemon, "api", fake.path)
    req = ask(tcp, web, "run_tests")
    assert req["status"] == "awaiting_grant"
    time.sleep(SETTLE_S)
    assert fake.messages == []

    r = human.post(f"/v1/approvals/{req['request_id']}", json={"scope": "once"})
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "queued"
    messages = fake.wait_for(1)
    assert len(messages) == 1
    assert_wake_message(messages[0], req["request_id"])


def test_idle_wake_off_posts_nothing_and_stop_delivers(short_home, fake):
    with (
        make_daemon(short_home, idle_wake=False) as d,
        httpx.Client(base_url=d.base_url, timeout=15) as tcp,
    ):
        web = register(tcp, d, "web")
        api = register(tcp, d, "api", fake.path)
        req = ask(tcp, web)
        claimed = stop_claim(tcp, api)
        assert [r["request_id"] for r in claimed] == [req["request_id"]]
        time.sleep(SETTLE_S)
        assert fake.messages == []


def test_the_session_start_hook_file_carries_the_socket_to_a_heartbeat(
    tcp, wake_daemon, fake
):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api")
    pid = os.getpid()
    from typer.testing import CliRunner

    from covey.cli import app

    env = {
        "COVEY_HOME": str(wake_daemon.cfg.home),
        "COVEY_PORT": str(wake_daemon.cfg.port),
        "CLAUDE_PID": str(pid),
        "CLAUDE_CODE_MESSAGING_SOCKET": fake.path,
    }
    result = CliRunner().invoke(
        app,
        ["hook", "session-start"],
        input=json.dumps({"session_id": "s1", "cwd": "/x/api"}),
        env=env,
    )
    assert result.exit_code == 0, result.output

    claude = {"pid": pid, "start_time": _start_time(pid)}
    carried = read_wake_file(wake_daemon.cfg, claude)
    assert carried == fake.path
    heartbeat(tcp, api, carried)
    req = ask(tcp, web)
    messages = fake.wait_for(1)
    assert len(messages) == 1
    assert_wake_message(messages[0], req["request_id"])


RESULT = "SECRET-RESULT-TEXT-do-not-leak"


@pytest.fixture
def asker_fake(short_home):
    session = FakeSession(short_home / "web.sock")
    yield session
    session.close()


def reply(tcp, api, rid, status="done"):
    r = tcp.post(
        f"/v1/requests/{rid}/reply",
        json={"result": RESULT, "status": status},
        headers=api,
    )
    assert r.status_code == 200, r.text
    return r


def assert_reply_message(lines, rid, status):
    assert len(lines) == 2
    assert lines[0]["type"] == "auth"
    content = lines[1]["message"]["content"]
    assert FRAME in content and "covey_wait" in content
    assert "api" in content and rid in content and status in content
    assert RESULT not in content and TASK not in content


def reply_posted_rows(human, rid):
    events = human.get("/v1/log", params={"thread": rid}).json()["events"]
    return [e for e in events if e["event"] == "request.reply_posted"]


def wait_for_reply_posted(human, rid, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if rows := reply_posted_rows(human, rid):
            return rows
        time.sleep(0.05)
    return []


def test_a_reply_nobody_waits_on_is_posted_once_to_the_asker(
    tcp, human, wake_daemon, asker_fake
):
    web = register(tcp, wake_daemon, "web", asker_fake.path)
    api = register(tcp, wake_daemon, "api")
    req = ask(tcp, web)
    rid = req["request_id"]
    reply(tcp, api, rid)
    messages = asker_fake.wait_for(1)
    assert len(messages) == 1
    assert_reply_message(messages[0], rid, "done")
    rows = wait_for_reply_posted(human, rid)
    assert len(rows) == 1
    assert rows[0]["detail"] == {"agent": req["from_agent"]}
    assert rows[0]["actor"].startswith("coveyd@")

    reply(tcp, api, rid)  # an identical repeat is not a new status
    time.sleep(SETTLE_S)
    assert len(asker_fake.messages) == 1
    assert len(reply_posted_rows(human, rid)) == 1


def test_a_failed_reply_post_writes_no_audit_row(tcp, human, wake_daemon, short_home):
    web = register(tcp, wake_daemon, "web", str(short_home / "gone.sock"))
    api = register(tcp, wake_daemon, "api")
    rid = ask(tcp, web)["request_id"]
    reply(tcp, api, rid)
    time.sleep(SETTLE_S)
    assert reply_posted_rows(human, rid) == []


def test_a_reply_while_a_wait_is_open_posts_nothing(tcp, wake_daemon, asker_fake):
    web = register(tcp, wake_daemon, "web", asker_fake.path)
    api = register(tcp, wake_daemon, "api")
    rid = ask(tcp, web)["request_id"]
    got = []
    waiter = threading.Thread(
        target=lambda: got.append(
            tcp_get(wake_daemon, web, f"/v1/requests/{rid}?wait=10")
        )
    )
    waiter.start()
    time.sleep(SETTLE_S)  # the long-poll is open
    reply(tcp, api, rid)
    waiter.join(5)
    assert got and got[0]["request"]["status"] == "done"
    time.sleep(SETTLE_S)
    assert asker_fake.messages == []


def tcp_get(daemon, headers, path):
    with httpx.Client(base_url=daemon.base_url, timeout=15) as c:
        return c.get(path, headers=headers).json()


def test_an_asker_that_is_not_live_gets_no_post(tcp, wake_daemon, asker_fake):
    web = register(tcp, wake_daemon, "web", asker_fake.path)
    api = register(tcp, wake_daemon, "api")
    rid = ask(tcp, web)["request_id"]
    r = tcp.request(
        "DELETE", "/v1/agents/web/session", json={"reason": "logout"}, headers=web
    )
    assert r.status_code == 200, r.text
    reply(tcp, api, rid)
    time.sleep(SETTLE_S)
    assert asker_fake.messages == []


def test_an_asker_without_a_wake_socket_gets_no_post(tcp, wake_daemon, fake):
    web = register(tcp, wake_daemon, "web")
    api = register(tcp, wake_daemon, "api")
    rid = ask(tcp, web)["request_id"]
    reply(tcp, api, rid)
    time.sleep(SETTLE_S)
    assert fake.messages == []


def test_needs_input_is_posted(tcp, wake_daemon, asker_fake):
    web = register(tcp, wake_daemon, "web", asker_fake.path)
    api = register(tcp, wake_daemon, "api")
    rid = ask(tcp, web)["request_id"]
    reply(tcp, api, rid, "needs_input")
    messages = asker_fake.wait_for(1)
    assert len(messages) == 1
    assert_reply_message(messages[0], rid, "needs_input")


def test_idle_wake_off_posts_no_reply(short_home, asker_fake):
    with (
        make_daemon(short_home, idle_wake=False) as d,
        httpx.Client(base_url=d.base_url, timeout=15) as tcp,
    ):
        web = register(tcp, d, "web", asker_fake.path)
        api = register(tcp, d, "api")
        rid = ask(tcp, web)["request_id"]
        reply(tcp, api, rid)
        time.sleep(SETTLE_S)
        assert asker_fake.messages == []


def test_a_dead_asker_socket_is_dropped_on_a_reply(short_home, asker_fake):
    dead = short_home / "dead.sock"
    with (
        make_daemon(short_home) as d,
        httpx.Client(base_url=d.base_url, timeout=15) as tcp,
    ):
        web = register(tcp, d, "web", str(dead))
        api = register(tcp, d, "api")
        first = ask(tcp, web)["request_id"]
        reply(tcp, api, first)
        time.sleep(SETTLE_S)
        live = FakeSession(dead)  # a listener appears at the dropped path
        try:
            second = ask(tcp, web)["request_id"]
            reply(tcp, api, second)
            time.sleep(SETTLE_S)
            assert live.messages == []
        finally:
            live.close()
