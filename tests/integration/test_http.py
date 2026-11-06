"""The HTTP API end to end: a real daemon, two fake agents, TCP and unix socket."""

import concurrent.futures
import json
import time

import httpx
import pytest
from typer.testing import CliRunner

from covey.cli import app as cli_app


def auth(token):
    return {"Authorization": f"Bearer {token}"}


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


def register(tcp, daemon, slug, capabilities=()):
    body = {
        "slug": slug,
        "kind": "python",
        "delivery": "poll",
        "summary": f"agent {slug}",
        "capabilities": [{"name": n, "level": lvl} for n, lvl in capabilities],
    }
    r = tcp.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
    assert r.status_code == 200, r.text
    return r.json()["agent_token"]


@pytest.fixture
def web(tcp, daemon):
    return auth(register(tcp, daemon, "web", [("explain", "open")]))


@pytest.fixture
def api(tcp, daemon):
    caps = [
        ("explain", "open"),
        ("run_tests", "gated"),
        ("push", "forbidden"),
    ]
    return auth(register(tcp, daemon, "api", caps))


def ask(tcp, headers, to, capability, task="do it", **extra):
    return tcp.post(
        "/v1/requests",
        json={"to": to, "capability": capability, "task": task, **extra},
        headers=headers,
    )


def test_hero_flow_long_poll_returns_the_reply(tcp, web, api):
    r = ask(tcp, web, "api", "explain", "how does auth work?")
    assert r.status_code == 201
    request = r.json()["request"]
    assert request["status"] == "queued"
    rid = request["request_id"]

    def poll():
        started = time.monotonic()
        got = tcp.get(f"/v1/requests/{rid}", params={"wait": 10}, headers=web)
        return got, time.monotonic() - started

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(poll)
        time.sleep(0.3)
        inbox = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
        assert [x["request_id"] for x in inbox] == [rid]
        assert inbox[0]["status"] == "delivered"
        rep = tcp.post(
            f"/v1/requests/{rid}/reply",
            json={"result": "tokens in the header", "status": "done"},
            headers=api,
        )
        assert rep.status_code == 200
        got, elapsed = future.result(15)
    assert got.status_code == 200
    assert got.json()["request"]["status"] == "done"
    assert got.json()["request"]["result"] == "tokens in the header"
    assert elapsed < 5


def test_wait_returns_when_the_target_needs_input(tcp, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]

    def poll():
        started = time.monotonic()
        got = tcp.get(f"/v1/requests/{rid}", params={"wait": 10}, headers=web)
        return got, time.monotonic() - started

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(poll)
        time.sleep(0.3)
        tcp.get("/v1/agents/api/inbox", headers=api)
        rep = tcp.post(
            f"/v1/requests/{rid}/reply",
            json={"result": "which module?", "status": "needs_input"},
            headers=api,
        )
        assert rep.status_code == 200
        got, elapsed = future.result(15)
    assert got.json()["request"]["status"] == "needs_input"
    assert got.json()["request"]["result"] == "which module?"
    assert elapsed < 5


def test_wait_returns_after_the_timeout_when_nothing_happens(tcp, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    started = time.monotonic()
    got = tcp.get(f"/v1/requests/{rid}", params={"wait": 1}, headers=web)
    assert got.status_code == 200
    assert got.json()["request"]["status"] == "queued"
    assert 0.8 < time.monotonic() - started < 4


def test_gated_ask_needs_approval_and_a_once_grant_is_used_up(tcp, human, web, api):
    first = ask(tcp, web, "api", "run_tests").json()["request"]
    assert first["status"] == "awaiting_grant"

    pending = human.get("/v1/approvals").json()["requests"]
    assert [p["request_id"] for p in pending] == [first["request_id"]]

    answered = human.post(
        f"/v1/approvals/{first['request_id']}", json={"scope": "once"}
    )
    assert answered.status_code == 200
    assert answered.json()["request"]["status"] == "queued"
    assert answered.json()["grant"]["scope"] == "once"

    inbox = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
    assert [x["request_id"] for x in inbox] == [first["request_id"]]
    rep = tcp.post(
        f"/v1/requests/{first['request_id']}/reply",
        json={"result": "all green", "status": "done"},
        headers=api,
    )
    assert rep.status_code == 200

    second = ask(tcp, web, "api", "run_tests").json()["request"]
    assert second["status"] == "awaiting_grant"
    assert human.get("/v1/grants").json()["grants"] == []


def test_approvals_long_poll_wakes_when_an_approval_appears(tcp, human, web, api):
    def poll():
        return human.get("/v1/approvals", params={"wait": 10}).json()["requests"]

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(poll)
        time.sleep(0.3)
        rid = ask(tcp, web, "api", "run_tests").json()["request"]["request_id"]
        started = time.monotonic()
        pending = future.result(15)
    assert [p["request_id"] for p in pending] == [rid]
    assert time.monotonic() - started < 5


def test_denying_an_approval_refuses_the_request(tcp, human, web, api):
    rid = ask(tcp, web, "api", "run_tests").json()["request"]["request_id"]
    r = human.post(f"/v1/approvals/{rid}", json={"scope": "deny"})
    assert r.status_code == 200
    assert r.json()["request"]["status"] == "refused"
    again = human.post(f"/v1/approvals/{rid}", json={"scope": "once"})
    assert again.status_code == 409


def test_forbidden_capability_is_created_refused(tcp, web, api):
    r = ask(tcp, web, "api", "push")
    assert r.status_code == 201
    assert r.json()["request"]["status"] == "refused"


def test_hops_limit_refuses_the_sixth_request_in_a_chain(tcp, web, api):
    pair = [(web, "api"), (api, "web")]
    parent = None
    for i in range(5):
        headers, to = pair[i % 2]
        extra = {"parent_id": parent} if parent else {}
        r = ask(tcp, headers, to, "explain", **extra)
        assert r.status_code == 201, r.text
        parent = r.json()["request"]["request_id"]
        assert r.json()["request"]["hops"] == i
    headers, to = pair[1]
    r = ask(tcp, headers, to, "explain", parent_id=parent)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "hops_exceeded"


def test_human_token_over_tcp_is_refused(tcp, daemon):
    r = tcp.get("/v1/whoami", headers=auth(daemon.human_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "human_only"


def test_join_token_cannot_ask(tcp, daemon, api):
    r = ask(tcp, auth(daemon.join_token), "api", "explain")
    assert r.status_code == 403


def test_missing_token_is_401(tcp):
    assert tcp.get("/v1/whoami").status_code == 401


def test_agent_cannot_read_another_agents_inbox(tcp, web, api):
    r = tcp.get("/v1/agents/api/inbox", headers=web)
    assert r.status_code == 403


def test_agent_cannot_read_a_request_between_other_agents(tcp, daemon, web, api):
    other = auth(register(tcp, daemon, "other", [("explain", "open")]))
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    assert tcp.get(f"/v1/requests/{rid}", headers=other).status_code == 403
    assert tcp.get(f"/v1/requests/{rid}", headers=api).status_code == 200


def test_agent_cannot_update_another_agents_card(tcp, web, api):
    r = tcp.put("/v1/agents/api/card", json={"summary": "mine now"}, headers=web)
    assert r.status_code == 403
    own = tcp.put("/v1/agents/web/card", json={"summary": "new"}, headers=web)
    assert own.status_code == 200
    assert own.json()["card"]["summary"] == "new"


def test_agent_cannot_create_or_revoke_grants_or_see_approvals(tcp, web, api):
    body = {
        "requester": "web",
        "target": "api",
        "capability": "run_tests",
        "scope": "always",
    }
    assert tcp.post("/v1/grants", json=body, headers=web).status_code == 403
    assert tcp.get("/v1/approvals", headers=web).status_code == 403
    gid = "01J00000000000000000000000"
    assert tcp.delete(f"/v1/grants/{gid}", headers=web).status_code == 403


def test_human_creates_lists_and_revokes_a_grant(tcp, human, web, api):
    body = {
        "requester": "web",
        "target": "api",
        "capability": "run_tests",
        "scope": "always",
    }
    r = human.post("/v1/grants", json=body)
    assert r.status_code == 201
    grant = r.json()["grant"]
    assert grant["created_by"].startswith("human@")
    assert grant["requester"].startswith("web@")

    # an agent sees grants that involve it, a stranger sees none
    seen = tcp.get("/v1/grants", headers=web).json()["grants"]
    assert [g["grant_id"] for g in seen] == [grant["grant_id"]]

    # with the grant in place the gated ask goes straight through
    assert ask(tcp, web, "api", "run_tests").json()["request"]["status"] == "queued"

    assert human.delete(f"/v1/grants/{grant['grant_id']}").status_code == 200
    assert human.get("/v1/grants").json()["grants"] == []
    assert human.delete(f"/v1/grants/{grant['grant_id']}").status_code == 404


def test_grant_for_an_unknown_slug_is_404(human, web):
    body = {
        "requester": "web",
        "target": "nobody",
        "capability": "x",
        "scope": "always",
    }
    assert human.post("/v1/grants", json=body).status_code == 404


def test_grant_with_an_unknown_field_is_400(human, web, api):
    body = {
        "requester": "web",
        "target": "api",
        "capability": "x",
        "scope": "always",
        "extra": 1,
    }
    assert human.post("/v1/grants", json=body).status_code == 400


def test_registering_a_live_slug_is_409(tcp, daemon, web):
    r = tcp.post(
        "/v1/agents/register",
        json={"slug": "web", "kind": "python", "delivery": "poll"},
        headers=auth(daemon.join_token),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "slug_live"


def test_agent_token_cannot_register(tcp, web):
    r = tcp.post(
        "/v1/agents/register",
        json={"slug": "z", "kind": "python", "delivery": "poll"},
        headers=web,
    )
    assert r.status_code == 403


def test_heartbeat_with_join_token_or_own_token_but_not_anothers(tcp, daemon, web, api):
    assert (
        tcp.post("/v1/agents/web/heartbeat", headers=auth(daemon.join_token))
    ).status_code == 200
    assert tcp.post("/v1/agents/web/heartbeat", headers=web).status_code == 200
    assert tcp.post("/v1/agents/api/heartbeat", headers=web).status_code == 403
    r = tcp.post("/v1/agents/ghost/heartbeat", headers=auth(daemon.join_token))
    assert r.status_code == 404


def test_identical_reply_is_idempotent_and_a_different_one_conflicts(tcp, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    body = {"result": "because", "status": "done"}
    url = f"/v1/requests/{rid}/reply"
    assert tcp.post(url, json=body, headers=api).status_code == 200
    again = tcp.post(url, json=body, headers=api)
    assert again.status_code == 200
    assert again.json()["request"]["result"] == "because"
    other = tcp.post(url, json={"result": "no", "status": "done"}, headers=api)
    assert other.status_code == 409
    # and only the target may reply
    assert tcp.post(url, json=body, headers=web).status_code == 403


def test_reply_with_a_bad_status_is_400(tcp, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    r = tcp.post(
        f"/v1/requests/{rid}/reply",
        json={"result": "x", "status": "queued"},
        headers=api,
    )
    assert r.status_code == 400


def test_ask_for_an_unknown_agent_is_404_and_empty_task_is_400(tcp, web):
    assert ask(tcp, web, "ghost", "explain").status_code == 404
    assert ask(tcp, web, "web", "explain", task="").status_code == 400


def test_find_lists_non_gone_agents_and_ranks_with_need(tcp, web, api):
    listed = tcp.get("/v1/agents", headers=web).json()["matches"]
    assert sorted(m["card"]["slug"] for m in listed) == ["api", "web"]
    ranked = tcp.get(
        "/v1/agents", params={"need": "run_tests", "limit": 1}, headers=web
    ).json()["matches"]
    assert [m["card"]["slug"] for m in ranked] == ["api"]


def test_whoami_for_an_agent(tcp, web):
    body = tcp.get("/v1/whoami", headers=web).json()
    assert body["card"]["slug"] == "web"
    assert body["covey"] == "1"


def test_human_can_ask_and_read_any_inbox(tcp, human, api):
    r = human.post(
        "/v1/requests",
        json={"to": "api", "capability": "explain", "task": "hello from me"},
    )
    assert r.status_code == 201
    assert r.json()["request"]["from_agent"].startswith("human@")
    inbox = human.get("/v1/agents/api/inbox").json()["requests"]
    assert [x["task"] for x in inbox] == ["hello from me"]


def test_log_scopes_agents_to_their_threads(tcp, daemon, human, web, api):
    other = auth(register(tcp, daemon, "other", [("explain", "open")]))
    rid = ask(tcp, web, "api", "explain", "visible task").json()["request"][
        "request_id"
    ]
    ask(tcp, other, "api", "explain", "private task")

    mine = tcp.get("/v1/log", headers=web).json()
    assert [r["request_id"] for r in mine["requests"]] == [rid]
    assert all(e["covey"] == "1" for e in mine["events"])
    everything = human.get("/v1/log").json()
    assert len(everything["requests"]) == 2
    one = human.get("/v1/log", params={"thread": rid}).json()
    assert [r["request_id"] for r in one["requests"]] == [rid]


def test_covey_log_cli_shows_the_thread(tcp, daemon, web, api, monkeypatch):
    rid = ask(tcp, web, "api", "explain", "explain the cache").json()["request"][
        "request_id"
    ]
    tcp.post(
        f"/v1/requests/{rid}/reply",
        json={"result": "it is LRU", "status": "done"},
        headers=api,
    )
    monkeypatch.setenv("COVEY_HOME", str(daemon.cfg.home))
    monkeypatch.setenv("COVEY_PORT", str(daemon.cfg.port))
    res = CliRunner().invoke(cli_app, ["log"])
    assert res.exit_code == 0, res.output
    assert f"thread {rid}" in res.output
    assert "web -> api" in res.output
    assert "it is LRU" in res.output
    as_json = CliRunner().invoke(cli_app, ["log", "--json", "--thread", rid])
    assert json.loads(as_json.output)["requests"][0]["status"] == "done"


def test_every_response_carries_the_version(tcp, human, web):
    for r in (
        tcp.get("/v1/whoami", headers=web),
        tcp.get("/v1/whoami"),
        human.get("/v1/approvals"),
        tcp.get("/v1/nope", headers=web),
    ):
        assert r.json()["covey"] == "1"


def test_human_peeking_at_an_inbox_does_not_deliver(tcp, human, web, api):
    rid = ask(tcp, web, "api", "explain").json()["request"]["request_id"]
    peek = human.get("/v1/agents/api/inbox").json()["requests"]
    assert [r["status"] for r in peek] == ["queued"]
    mine = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
    assert [(r["request_id"], r["status"]) for r in mine] == [(rid, "delivered")]


@pytest.mark.parametrize("expires_at", ["never", "0", "2026-13-01T00:00:00Z"])
def test_grant_expiry_must_be_a_date_time(human, web, api, expires_at):
    body = {"requester": "web", "target": "api", "capability": "run_tests"}
    r = human.post(
        "/v1/grants", json={**body, "scope": "always", "expires_at": expires_at}
    )
    assert r.status_code == 400


def test_grant_expiry_is_stored_in_utc(human, web, api):
    body = {
        "requester": "web",
        "target": "api",
        "capability": "run_tests",
        "scope": "always",
    }
    r = human.post(
        "/v1/grants", json={**body, "expires_at": "2030-01-01T02:00:00+02:00"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["grant"]["expires_at"] == "2030-01-01T00:00:00.000Z"
