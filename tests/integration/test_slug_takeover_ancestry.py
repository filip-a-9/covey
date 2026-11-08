"""A join-token register on a human-created or granted slug: allowed only from outside every
other agent's process tree and, if the human set a workdir, from inside it (S1)."""

import sys

import httpx
import pytest

from tests.integration.procs import register_from, register_inside


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


@pytest.fixture
def fake_claude(tmp_path):
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(sys.executable)
    return link


SHIM = {"kind": "claude-code", "delivery": "hooks"}


def add(human, workdir, slug="docs"):
    body = {"slug": slug, "workdir": str(workdir), "wakeable": True}
    assert human.post("/v1/agents", json=body).status_code == 201


def end_session(tcp, token, slug):
    r = tcp.request(
        "DELETE",
        f"/v1/agents/{slug}/session",
        json={"reason": "x"},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text


def code(result):
    status, data = result
    return status, data.get("error", {}).get("code")


def test_a_human_created_slug_takes_session_after_session(daemon, tcp, human, tmp_path):
    work = tmp_path / "docs"
    work.mkdir()
    add(human, work)
    body = {"slug": "docs", **SHIM}
    first = register_from(daemon, body, work, tmp_path)
    assert first[0] == 200, first
    end_session(tcp, first[1]["agent_token"], "docs")
    sub = work / "pkg"
    sub.mkdir()
    second = register_from(daemon, body, sub, tmp_path)  # a new process, inside workdir
    assert second[0] == 200, second


def test_a_register_from_another_agents_tree_is_refused(
    daemon, human, tmp_path, fake_claude
):
    work = tmp_path / "docs"
    work.mkdir()
    add(human, work)
    other, inner = register_inside(
        daemon,
        {"slug": "other", **SHIM},
        {"slug": "docs", **SHIM},
        work,
        tmp_path,
        fake_claude,
    )
    assert other[0] == 200
    assert code(inner) == (409, "slug_owned")


def test_a_register_from_the_wrong_directory_is_refused(daemon, human, tmp_path):
    work = tmp_path / "docs"
    elsewhere = tmp_path / "elsewhere"
    work.mkdir()
    elsewhere.mkdir()
    add(human, work)
    got = register_from(daemon, {"slug": "docs", **SHIM}, elsewhere, tmp_path)
    assert code(got) == (409, "slug_owned")


def test_a_register_over_tcp_on_a_human_created_slug_is_refused(
    daemon, tcp, human, tmp_path
):
    add(human, tmp_path)
    r = tcp.post(
        "/v1/agents/register",
        json={"slug": "docs", **SHIM},
        headers=auth(daemon.join_token),
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "slug_owned")


def test_a_granted_shim_slug_follows_only_the_ancestry_rule(
    daemon, tcp, human, tmp_path, fake_claude
):
    held = register_from(daemon, {"slug": "held", **SHIM}, tmp_path, tmp_path)
    peer = register_from(daemon, {"slug": "peer", **SHIM}, tmp_path, tmp_path)
    assert held[0] == peer[0] == 200
    g = {"requester": "held", "target": "peer", "capability": "x", "scope": "always"}
    assert human.post("/v1/grants", json=g).status_code == 201
    end_session(tcp, held[1]["agent_token"], "held")
    _, inside = register_inside(
        daemon,
        {"slug": "other", **SHIM},
        {"slug": "held", **SHIM},
        tmp_path,
        tmp_path,
        fake_claude,
    )
    assert code(inside) == (409, "slug_owned")
    outside = register_from(daemon, {"slug": "held", **SHIM}, tmp_path, tmp_path)
    assert outside[0] == 200, outside
