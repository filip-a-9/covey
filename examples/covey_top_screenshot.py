"""Render `covey top` at 100x30 against a real daemon and save examples/screenshots/covey-top.svg.

Run from the repo root: `uv run python examples/covey_top_screenshot.py`.
Seeds two agents, one answered request and one gated request waiting for approval.
"""

import asyncio
import socket
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from covey.config import Config
from covey.tui.app import CoveyTop
from tests.integration.conftest import run_daemon

OUT = ROOT / "examples" / "screenshots" / "covey-top.svg"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(base_url: str, join_token: str) -> None:
    def card(slug, summary, caps):
        return {
            "slug": slug,
            "kind": "claude-code",
            "delivery": "poll",
            "summary": summary,
            "capabilities": [{"name": n, "level": lvl} for n, lvl in caps],
        }

    with httpx.Client(base_url=base_url) as c:
        tokens = {}
        for body in (
            card("web", "React storefront client", [("explain", "open")]),
            card(
                "api",
                "Billing API: owns the test suite and the schema",
                [("explain", "open"), ("run_tests", "gated")],
            ),
        ):
            r = c.post(
                "/v1/agents/register",
                json=body,
                headers={"Authorization": f"Bearer {join_token}"},
            )
            tokens[body["slug"]] = {
                "Authorization": f"Bearer {r.json()['agent_token']}"
            }

        def ask(capability, task):
            r = c.post(
                "/v1/requests",
                json={"to": "api", "capability": capability, "task": task},
                headers=tokens["web"],
            )
            return r.json()["request"]["request_id"]

        explained = ask("explain", "how does the invoice total get rounded?")
        c.get("/v1/agents/api/inbox", headers=tokens["api"])
        c.post(
            f"/v1/requests/{explained}/reply",
            json={
                "status": "done",
                "result": "Totals round half-even in billing/money.py.",
            },
            headers=tokens["api"],
        )
        ask("run_tests", "run pytest against the client change")


async def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="covey-", dir="/tmp"))
    cfg = Config(home=home / "h", port=free_port(), sweep_interval_s=0.2)
    with run_daemon(cfg) as daemon:
        await asyncio.to_thread(seed, daemon.base_url, daemon.join_token)
        app = CoveyTop(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(app.export_screenshot(title="covey top"))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
