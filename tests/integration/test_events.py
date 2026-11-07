"""GET /v1/events: the human's live audit stream over the unix socket."""

import json
import threading

import httpx
import pytest

from tests.integration.test_http import ask, auth, register


@pytest.fixture
def tcp(daemon):
    with httpx.Client(base_url=daemon.base_url, timeout=15) as c:
        yield c


@pytest.fixture
def human(daemon):
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers=auth(daemon.human_token),
        timeout=15,
    ) as c:
        yield c


def read_events(lines, want):
    """Collect (id, data) from an SSE line iterator until `want(data)` is true."""
    event_id = None
    for line in lines:
        if line.startswith("id:"):
            event_id = int(line[3:])
        elif line.startswith("data:"):
            data = json.loads(line[5:])
            if want(data):
                return event_id, data
    raise AssertionError("stream ended without the wanted event")


def test_request_created_after_subscribing_is_streamed(daemon, tcp, human):
    web = auth(register(tcp, daemon, "web", [("explain", "open")]))
    register(tcp, daemon, "api", [("explain", "open")])
    created = {}

    def create_later():
        created["id"] = ask(tcp, web, "api", "explain").json()["request"]["request_id"]

    with human.stream("GET", "/v1/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        threading.Timer(0.3, create_later).start()
        event_id, data = read_events(
            r.iter_lines(), lambda d: d["event"] == "request.created"
        )
    assert data["subject"] == created["id"]
    assert data["covey"]
    assert event_id == data["id"]


def test_last_event_id_replays_later_rows(daemon, tcp, human):
    web = auth(register(tcp, daemon, "web", [("explain", "open")]))
    register(tcp, daemon, "api", [("explain", "open")])
    ask(tcp, web, "api", "explain")
    first = human.get("/v1/log").json()["events"][0]["id"]
    headers = {"Last-Event-ID": str(first - 1)}
    with human.stream("GET", "/v1/events", headers=headers) as r:
        _, data = read_events(r.iter_lines(), lambda d: True)
    assert data["id"] == first


def test_agent_token_is_refused(daemon, tcp):
    web = auth(register(tcp, daemon, "web", [("explain", "open")]))
    assert tcp.get("/v1/events", headers=web).status_code == 403


def test_human_token_over_tcp_is_refused(daemon, tcp):
    r = tcp.get("/v1/events", headers=auth(daemon.human_token))
    assert r.status_code == 403
