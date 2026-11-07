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
