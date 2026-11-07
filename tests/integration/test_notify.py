"""A gated request notifies the human only when nobody is watching."""

import threading
import time

import httpx
import pytest

from covey.daemon import notify
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


@pytest.fixture
def web(tcp, daemon):
    return auth(register(tcp, daemon, "web", [("explain", "open")]))


@pytest.fixture
def api(tcp, daemon):
    caps = [("explain", "open"), ("run_tests", "gated")]
    return auth(register(tcp, daemon, "api", caps))


class Recorder:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, text):
        self.texts.append(text)


@pytest.fixture
def sent(daemon):  # after the daemon starts: it configures notify
    rec = Recorder()
    notify.use_backend(rec)
    yield rec
    notify.reset()


def gated(tcp, web):
    return ask(tcp, web, "api", "run_tests").json()["request"]["request_id"]


def test_gated_request_with_no_watcher_notifies_with_its_id(tcp, web, api, sent):
    rid = gated(tcp, web)
    notify.drain()
    assert len(sent.texts) == 1
    assert rid in sent.texts[0]
    assert f"covey approve {rid}" in sent.texts[0]


def test_open_request_does_not_notify(tcp, web, api, sent):
    ask(tcp, web, "api", "explain")
    notify.drain()
    assert sent.texts == []


def test_gated_request_with_an_events_stream_open_does_not_notify(
    tcp,
    human,
    web,
    api,
    sent,
):
    with human.stream("GET", "/v1/events") as r:
        lines = r.iter_lines()  # kept: a dropped iterator closes the stream
        next(lines)  # the stream has started
        gated(tcp, web)
        notify.drain()
        assert sent.texts == []


def test_gated_request_with_an_approvals_long_poll_open_does_not_notify(
    tcp,
    human,
    web,
    api,
    sent,
):
    poll = threading.Thread(
        target=lambda: human.get("/v1/approvals", params={"wait": 2}), daemon=True
    )
    poll.start()
    time.sleep(0.5)  # let the poll reach the daemon
    # The poll returns as soon as a request is pending, but the request was created first.
    gated(tcp, web)
    notify.drain()
    assert sent.texts == []
    poll.join(5)


def test_after_the_stream_closes_the_next_gated_request_notifies(
    tcp,
    human,
    web,
    api,
    sent,
):
    with human.stream("GET", "/v1/events") as r:
        lines = r.iter_lines()
        next(lines)
    deadline = time.monotonic() + 5
    while not sent.texts and time.monotonic() < deadline:
        gated(tcp, web)  # the daemon notices the disconnect a moment after the close
        notify.drain()
        time.sleep(0.1)
    assert sent.texts
