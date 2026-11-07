"""Worker tokens: a headless worker acts as its agent without registering or going live."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from covey.config import Config
from covey.daemon import registry
from covey.daemon.app import create_app


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app(tmp_path):
    # No dispatch: these tests mint the worker token by hand and need the request queued.
    cfg = Config(home=tmp_path / "h", port=7777, max_workers=0)
    return create_app(
        replace(cfg, claude_bin="/nonexistent/claude-is-disabled-in-tests")
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def daemon(app):
    return app.state.daemon


def register(client, daemon, slug):
    join = daemon.cfg.join_token_path.read_text().strip()
    body = {
        "slug": slug,
        "kind": "python",
        "delivery": "poll",
        "capabilities": [{"name": "explain", "level": "open"}],
    }
    r = client.post("/v1/agents/register", json=body, headers=auth(join))
    assert r.status_code == 200, r.text
    return auth(r.json()["agent_token"])


@pytest.fixture
def web(client, daemon):
    return register(client, daemon, "web")


@pytest.fixture
def docs_request(client, daemon, web):
    """`docs` is wakeable with no session, and `web` has asked it something."""
    registry.create_agent(
        daemon.conn,
        daemon.node.node_id,
        {
            "slug": "docs",
            "workdir": "/x",
            "wakeable": True,
            "capabilities": [{"name": "explain", "level": "open"}],
        },
    )
    r = client.post(
        "/v1/requests",
        json={"to": "docs", "capability": "explain", "task": "what is X?"},
        headers=web,
    )
    assert r.status_code == 201, r.text
    return r.json()["request"]


@pytest.fixture
def worker(daemon, docs_request):
    token = daemon.mint_worker_token(
        docs_request["to_agent"], docs_request["request_id"]
    )
    return auth(token)


def liveness(daemon, slug):
    row = daemon.conn.execute(
        "SELECT liveness FROM agents WHERE slug = ?", (slug,)
    ).fetchone()
    return row["liveness"]


def test_worker_token_acts_as_its_agent(client, worker):
    r = client.get("/v1/whoami", headers=worker)
    assert r.status_code == 200, r.text
    assert r.json()["card"]["slug"] == "docs"


def test_worker_can_reply_to_its_request(client, worker, docs_request):
    rid = docs_request["request_id"]
    r = client.post(
        f"/v1/requests/{rid}/reply",
        json={"result": "X is Y.", "status": "done"},
        headers=worker,
    )
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "done"


def test_worker_cannot_heartbeat_or_attach_a_session(client, daemon, worker):
    beat = client.post("/v1/agents/docs/heartbeat", headers=worker)
    attach = client.put(
        "/v1/agents/docs/session", json={"session_id": "s1"}, headers=worker
    )
    end = client.request(
        "DELETE", "/v1/agents/docs/session", json={"reason": "x"}, headers=worker
    )
    assert (beat.status_code, attach.status_code, end.status_code) == (403, 403, 403)
    assert liveness(daemon, "docs") == "wakeable"


def test_worker_ask_without_a_thread_continues_its_request(
    client, worker, docs_request
):
    r = client.post(
        "/v1/requests",
        json={"to": "web", "capability": "explain", "task": "what does web mean by X?"},
        headers=worker,
    )
    assert r.status_code == 201, r.text
    sub = r.json()["request"]
    assert sub["parent_id"] == docs_request["request_id"]
    assert sub["thread_id"] == docs_request["thread_id"]
    assert sub["hops"] == docs_request["hops"] + 1


def test_revoked_worker_token_is_401(client, daemon, worker):
    token = worker["Authorization"].split()[1]
    daemon.revoke_worker_token(token)
    assert client.get("/v1/whoami", headers=worker).status_code == 401


def test_a_real_session_can_register_while_a_worker_runs(client, daemon, worker):
    row = daemon.conn.execute("SELECT card FROM agents WHERE slug = 'docs'").fetchone()
    card = json.loads(row["card"])  # the human re-adds `docs`: approves the takeover
    registry.create_agent(daemon.conn, daemon.node.node_id, {**card, "wakeable": True})
    register(client, daemon, "docs")
    assert liveness(daemon, "docs") == "live"
    assert client.get("/v1/whoami", headers=worker).status_code == 200
