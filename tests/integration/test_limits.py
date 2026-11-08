"""Request limits: body size, field sizes, open gated requests per requester, Host on TCP."""

import httpx
import pytest

from tests.integration.test_http import ask, auth, register

MIB = 1024 * 1024


@pytest.fixture
def tcp(daemon):
    with httpx.Client(base_url=daemon.base_url, timeout=15) as c:
        yield c


@pytest.fixture
def web(tcp, daemon):
    return auth(register(tcp, daemon, "web", [("explain", "open")]))


@pytest.fixture
def api(tcp, daemon):
    caps = [("explain", "open"), ("run_tests", "gated")]
    return auth(register(tcp, daemon, "api", caps))


def code_of(res):
    return res.json()["error"]["code"]


def test_oversized_content_length_is_refused_before_auth(tcp):
    # The shape a web page can send without a preflight: no token, text/plain.
    res = tcp.post(
        "/v1/requests",
        content=b"x" * (2 * MIB),
        headers={"Content-Type": "text/plain", "Origin": "http://evil.example"},
    )
    assert res.status_code == 413
    assert code_of(res) == "body_too_large"


def test_oversized_chunked_body_is_refused(tcp):
    def chunks():
        for _ in range(4):
            yield b"x" * (512 * 1024)

    res = tcp.post(
        "/v1/requests", content=chunks(), headers={"Content-Type": "text/plain"}
    )
    assert res.status_code == 413


def test_a_normal_request_still_passes(tcp, web, api):
    assert ask(tcp, web, "api", "explain", "x" * 1000).status_code == 201


@pytest.mark.parametrize("field", ["task", "context"])
def test_oversized_ask_field_is_a_400(tcp, web, api, field):
    extra = {field: "x" * (64 * 1024 + 1)}
    res = ask(tcp, web, "api", "explain", **extra)
    assert res.status_code == 400
    assert code_of(res) == "bad_request"


def test_ask_field_at_the_cap_is_accepted(tcp, web, api):
    assert ask(tcp, web, "api", "explain", "x" * (64 * 1024)).status_code == 201


def test_oversized_reply_is_a_400(tcp, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    for body in (
        {"result": "x" * (64 * 1024 + 1), "status": "done"},
        {
            "result": "ok",
            "status": "done",
            "artifacts": [{"kind": "log", "path_or_url": "p"}] * 100,
        },
    ):
        res = tcp.post(f"/v1/requests/{rid}/reply", json=body, headers=api)
        assert res.status_code == 400, res.text


def test_open_gated_requests_per_requester_are_capped(tcp, web, api):
    for i in range(10):
        assert ask(tcp, web, "api", "run_tests", f"t{i}").status_code == 201
    res = ask(tcp, web, "api", "run_tests", "one too many")
    assert res.status_code == 429
    assert code_of(res) == "too_many_pending_approvals"
    # Open requests are not gated, so they still go through.
    assert ask(tcp, web, "api", "explain").status_code == 201


def test_another_requester_is_not_affected_by_the_cap(tcp, daemon, web, api):
    other = auth(register(tcp, daemon, "other", [("explain", "open")]))
    for i in range(10):
        ask(tcp, web, "api", "run_tests", f"t{i}")
    assert ask(tcp, other, "api", "run_tests").status_code == 201


def test_foreign_host_header_is_refused_on_tcp(tcp, web, daemon):
    res = tcp.get("/v1/whoami", headers={**web, "Host": "evil.example"})
    assert res.status_code == 403
    assert code_of(res) == "bad_host"


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_host_headers_are_accepted_on_tcp(tcp, web, daemon, name):
    host = f"{name}:{daemon.cfg.port}"
    assert tcp.get("/v1/whoami", headers={**web, "Host": host}).status_code == 200


def test_host_header_is_not_checked_on_the_unix_socket(daemon):
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers=auth(daemon.human_token),
    ) as c:
        assert c.get("/v1/whoami").status_code == 200
