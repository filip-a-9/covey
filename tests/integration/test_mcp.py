"""The `covey mcp` shim, driven through the MCP SDK's in-process client against a real daemon."""

import asyncio
import contextlib
import json
import re
import shutil
import stat
import subprocess
import sys

import httpx
from mcp import Client

from covey.config import Config
from covey.mcp.server import Shim, build_server
from tests.integration.conftest import free_port, run_daemon


@contextlib.asynccontextmanager
async def shim_client(cfg: Config, slug: str, **kw):
    kw.setdefault("start_daemon", False)
    shim = Shim(cfg, slug=slug, workdir=f"/code/{slug}", **kw)
    async with Client(build_server(shim)) as client:
        yield client


async def call(client: Client, tool: str, **args):
    """(is_error, text, envelope): the envelope is the JSON block that ends every result."""
    result = await client.call_tool(tool, args)
    text = result.content[0].text
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    return result.is_error, text, json.loads(blocks[-1]) if blocks else None


# Undeclared capabilities need a grant, so a target that answers questions declares this.
EXPLAIN = {"name": "explain", "level": "open"}


def human(daemon) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers={"Authorization": f"Bearer {daemon.human_token}"},
    )


async def test_hero_flow_over_mcp(daemon):
    async with (
        shim_client(daemon.cfg, "web") as web,
        shim_client(daemon.cfg, "api") as api,
    ):
        err, _, card = await call(
            api,
            "covey_describe",
            summary="Owns the orders API and its auth middleware.",
            tags=["orders", "auth"],
            capabilities=[{"name": "explain", "level": "open"}],
        )
        assert not err and card["card"]["summary"].startswith("Owns the orders API")

        err, _, found = await call(web, "covey_find", need="who owns the orders API")
        assert not err and found["matches"][0]["card"]["slug"] == "api"

        err, _, asked = await call(
            web, "covey_ask", to="api", capability="explain", task="How does auth work?"
        )
        assert not err and asked["request"]["status"] == "queued"
        rid = asked["request"]["request_id"]

        async def wait():
            return await call(web, "covey_wait", request_id=rid, timeout_s=10)

        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0.5)
        assert not waiter.done()  # really long-polling, not timing out early
        err, text, inbox = await call(api, "covey_inbox")
        assert not err and [r["request_id"] for r in inbox["requests"]] == [rid]
        assert "How does auth work?" in text
        assert "another agent" in text.split("How does auth work?")[0]

        err, _, replied = await call(
            api, "covey_reply", request_id=rid, result="Tokens in the header."
        )
        assert not err and replied["request"]["status"] == "done"

        err, text, waited = await asyncio.wait_for(waiter, 15)
        assert not err and waited["request"]["status"] == "done"
        assert "Tokens in the header." in text
        assert "another agent" in text.split("Tokens in the header.")[0]

        err, text, log = await call(
            web, "covey_log", thread_id=asked["request"]["thread_id"]
        )
        assert not err and [r["request_id"] for r in log["requests"]] == [rid]
        assert "another agent" in text.split("How does auth work?")[0]


async def test_whoami_shows_the_registered_card(daemon):
    async with shim_client(daemon.cfg, "web") as web:
        err, _, who = await call(web, "covey_whoami")
    assert not err
    assert who["card"]["slug"] == "web"
    assert who["card"]["kind"] == "claude-code"
    assert who["card"]["delivery"] == "hooks"
    assert who["card"]["workdir"] == "/code/web"
    assert who["liveness"] == "live"


async def test_grant_always_refuses_and_creates_nothing(daemon):
    async with shim_client(daemon.cfg, "web") as web:
        err, text, _ = await call(
            web, "covey_grant", requester="web", capability="run_tests", scope="always"
        )
    assert err
    assert "covey approve" in text
    with human(daemon) as h:
        assert h.get("/v1/grants").json()["grants"] == []


async def test_a_live_slug_keeps_the_shim_up_and_every_tool_names_covey_agent(daemon):
    r = await asyncio.to_thread(
        httpx.post,
        daemon.base_url + "/v1/agents/register",
        json={"slug": "web", "kind": "claude-code", "delivery": "hooks"},
        headers={"Authorization": f"Bearer {daemon.join_token}"},
    )
    assert r.status_code == 200
    async with shim_client(daemon.cfg, "web") as web:
        for tool, args in [
            ("covey_whoami", {}),
            ("covey_inbox", {}),
            ("covey_find", {"need": "x"}),
            ("covey_log", {}),
        ]:
            err, text, env = await call(web, tool, **args)
            assert err
            assert env["error"]["code"] == "slug_live"
            assert "COVEY_AGENT" in text


async def test_an_unreachable_daemon_is_an_error_that_says_covey_up(short_home):
    cfg = Config(home=short_home / "h", port=free_port())
    async with shim_client(cfg, "web") as web:
        err, text, env = await call(web, "covey_whoami")
    assert err
    assert env["error"]["code"] == "daemon_unreachable"
    assert "covey up" in text


async def test_a_wiped_daemon_is_rejoined_on_the_next_tool_call(short_home):
    # `rm -rf ~/.covey && covey up` under a running session: new node, new join token,
    # and the shim's agent token is unknown (401).
    cfg = Config(home=short_home / "h", port=free_port())
    with contextlib.ExitStack() as stack:
        stack.enter_context(run_daemon(cfg))
        async with shim_client(cfg, "web") as web:
            before = (await call(web, "covey_whoami"))[2]["node_id"]
            stack.close()
            shutil.rmtree(cfg.home)
            stack.enter_context(run_daemon(cfg))
            err, _, who = await call(web, "covey_whoami")
    assert not err
    assert who["card"]["slug"] == "web"
    assert who["node_id"] != before


async def test_the_heartbeat_rejoins_a_wiped_daemon_without_a_tool_call(short_home):
    cfg = Config(home=short_home / "h", port=free_port())
    with contextlib.ExitStack() as stack:
        stack.enter_context(run_daemon(cfg))
        async with shim_client(cfg, "web", heartbeat_s=0.2) as web:
            await call(web, "covey_whoami")  # registered before the wipe
            stack.close()
            shutil.rmtree(cfg.home)
            running = stack.enter_context(run_daemon(cfg))
            slugs: list[str] = []
            for _ in range(30):
                await asyncio.sleep(0.2)
                with human(running) as h:
                    agents = h.get("/v1/agents").json()["matches"]
                slugs = [m["card"]["slug"] for m in agents]
                if "web" in slugs:
                    break
    assert "web" in slugs


async def test_the_token_file_hooks_read_is_0600_and_removed_on_exit(daemon):
    claude = {"pid": 4242, "start_time": "777"}
    sessions = daemon.cfg.home / "sessions"
    sessions.mkdir(mode=0o755)
    (sessions / "claude-4242.tmp").write_text("left over")
    (sessions / "claude-4242.tmp").chmod(0o644)
    path = sessions / "claude-4242.json"
    async with shim_client(daemon.cfg, "web", claude=claude) as web:
        await call(web, "covey_whoami")  # registration has finished
        saved = json.loads(path.read_text())
        mode = stat.S_IMODE(path.stat().st_mode)
        dir_mode = stat.S_IMODE(sessions.stat().st_mode)
        r = await asyncio.to_thread(
            httpx.post,
            daemon.base_url + "/v1/agents/web/heartbeat",
            headers={"Authorization": f"Bearer {saved['agent_token']}"},
        )
    assert saved["slug"] == "web"
    assert saved["claude_start_time"] == "777"
    assert r.status_code == 200
    assert mode == 0o600
    assert dir_mode == 0o700
    assert not path.exists()


async def test_parallel_calls_after_a_wipe_rejoin_once_and_both_succeed(short_home):
    cfg = Config(home=short_home / "h", port=free_port())
    with contextlib.ExitStack() as stack:
        stack.enter_context(run_daemon(cfg))
        async with shim_client(cfg, "web") as web:
            await call(web, "covey_whoami")
            stack.close()
            shutil.rmtree(cfg.home)
            stack.enter_context(run_daemon(cfg))
            results = await asyncio.gather(
                call(web, "covey_whoami"),
                call(web, "covey_inbox"),
                call(web, "covey_log"),
            )
    assert [err for err, _, _ in results] == [False, False, False]


async def test_wait_above_600_seconds_is_capped_not_refused(daemon):
    async with (
        shim_client(daemon.cfg, "web") as web,
        shim_client(daemon.cfg, "api") as api,
    ):
        await call(api, "covey_describe", capabilities=[EXPLAIN])
        rid = (await call(web, "covey_ask", to="api", capability="explain", task="q"))[
            2
        ]["request"]["request_id"]
        await call(api, "covey_inbox")
        await call(api, "covey_reply", request_id=rid, result="a")
        err, _, waited = await call(web, "covey_wait", request_id=rid, timeout_s=900)
    assert not err and waited["request"]["status"] == "done"


async def test_peers_and_inbox_resources(daemon):
    async with (
        shim_client(daemon.cfg, "web") as web,
        shim_client(daemon.cfg, "api") as api,
    ):
        await call(
            api,
            "covey_describe",
            summary="Owns the orders API.",
            capabilities=[EXPLAIN],
        )
        await call(
            web, "covey_ask", to="api", capability="explain", task="Where is auth?"
        )
        peers = (await web.read_resource("covey://peers")).contents[0].text
        inbox = (await api.read_resource("covey://inbox")).contents[0].text
    assert "| `api` |" in peers and "Owns the orders API." in peers
    assert "Where is auth?" in inbox


def test_the_shim_does_not_import_the_daemon_or_fastapi():
    code = (
        "import sys, covey.mcp.server, covey.cli; "
        "bad = [m for m in sys.modules if m == 'fastapi' or m.startswith('covey.daemon')]; "
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_the_cli_does_not_import_the_mcp_sdk():
    code = "import sys, covey.cli; print('mcp' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


async def test_covey_mcp_serves_over_stdio_with_identity_from_the_environment(
    daemon, tmp_path
):
    from pathlib import Path

    from mcp import StdioServerParameters

    workdir = tmp_path / "Orders Service"
    workdir.mkdir()
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "covey"),
        args=["mcp"],
        env={"COVEY_HOME": str(daemon.cfg.home), "COVEY_PORT": str(daemon.cfg.port)},
        cwd=workdir,
    )
    async with Client(params) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        err, _, who = await call(client, "covey_whoami")
    assert tools == sorted(
        [
            "covey_whoami",
            "covey_describe",
            "covey_find",
            "covey_ask",
            "covey_wait",
            "covey_inbox",
            "covey_reply",
            "covey_grant",
            "covey_log",
        ]
    )
    assert not err
    assert who["card"]["slug"] == "orders-service"
    assert who["card"]["workdir"] == str(workdir)


async def test_an_unusable_directory_name_reports_covey_agent(daemon, tmp_path):
    from pathlib import Path

    from mcp import StdioServerParameters

    workdir = tmp_path / "---"
    workdir.mkdir()
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "covey"),
        args=["mcp"],
        env={"COVEY_HOME": str(daemon.cfg.home), "COVEY_PORT": str(daemon.cfg.port)},
        cwd=workdir,
    )
    async with Client(params) as client:
        err, text, _ = await call(client, "covey_whoami")
    assert err
    assert "COVEY_AGENT" in text
    with human(daemon) as h:
        assert h.get("/v1/agents").json()["matches"] == []


async def test_closing_the_shims_stdin_detaches_the_session_at_once(daemon, tmp_path):
    import sqlite3
    from pathlib import Path

    from mcp import StdioServerParameters

    workdir = tmp_path / "closer"
    workdir.mkdir()
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "covey"),
        args=["mcp"],
        env={
            "COVEY_HOME": str(daemon.cfg.home),
            "COVEY_PORT": str(daemon.cfg.port),
            "CLAUDE_CODE_SESSION_ID": "sess-1",
        },
        cwd=workdir,
    )

    def row():
        db = sqlite3.connect(daemon.cfg.db_path)
        try:
            return db.execute(
                "SELECT liveness, session_id FROM agents WHERE slug = 'closer'"
            ).fetchone()
        finally:
            db.close()

    async with Client(params) as client:
        await call(client, "covey_whoami")  # registration has finished
        assert row() == ("live", "sess-1")
    # leaving the context closed stdin and waited for the shim to exit
    assert row() == ("gone", "")
