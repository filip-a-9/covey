"""`covey top` mounts against a real daemon and lists its agents. Looks are checked by screenshot."""

import pytest

pytest.importorskip("textual")

from textual.widgets import OptionList

from covey.tui.app import CoveyTop
from tests.integration.test_http import auth


async def test_top_mounts_and_shows_registered_agent(daemon):
    import httpx

    body = {
        "slug": "web",
        "kind": "python",
        "delivery": "poll",
        "summary": "storefront",
        "capabilities": [{"name": "explain", "level": "open"}],
    }
    with httpx.Client(base_url=daemon.base_url) as c:
        r = c.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
        assert r.status_code == 200

    app = CoveyTop(daemon.cfg)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        ids = [o.id for o in app.query_one("#agents", OptionList).options]
    assert "web" in ids


async def test_pressing_o_on_a_pending_approval_creates_a_once_grant(daemon):
    import httpx

    def register(slug, caps):
        body = {
            "slug": slug,
            "kind": "python",
            "delivery": "poll",
            "summary": slug,
            "capabilities": [{"name": n, "level": lvl} for n, lvl in caps],
        }
        r = c.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
        assert r.status_code == 200
        return auth(r.json()["agent_token"])

    with httpx.Client(base_url=daemon.base_url) as c:
        web = register("web", [("explain", "open")])
        register("api", [("run_tests", "gated")])
        sent = c.post(
            "/v1/requests",
            json={"to": "api", "capability": "run_tests", "task": "run the suite"},
            headers=web,
        )
        rid = sent.json()["request"]["request_id"]
        assert sent.json()["request"]["status"] == "awaiting_grant"

        app = CoveyTop(daemon.cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("o")
            for _ in range(20):
                await pilot.pause(0.2)
                if not app.pending:
                    break

        with httpx.Client(
            transport=httpx.HTTPTransport(uds=daemon.socket_path),
            base_url="http://covey",
            headers=auth(daemon.human_token),
        ) as human:
            scopes = [g["scope"] for g in human.get("/v1/grants").json()["grants"]]
        status = c.get(f"/v1/requests/{rid}", headers=web).json()["request"]["status"]
    assert scopes == ["once"]
    assert status == "queued"
    assert not app.pending
