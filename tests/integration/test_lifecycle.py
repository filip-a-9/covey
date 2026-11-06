"""Daemon lifecycle: in-process serve(), and `covey up/status/down` against a real detached daemon."""

import json
import os
import socket
import stat
import subprocess
import sys

import httpx
import pytest
from typer.testing import CliRunner

from covey.cli import app
from tests.integration.conftest import free_port


def unix_client(path: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=path), base_url="http://covey"
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- serve() in-process -------------------------------------------------------------


def test_socket_is_mode_0600(daemon):
    mode = stat.S_IMODE(os.stat(daemon.socket_path).st_mode)
    assert mode == 0o600


def test_whoami_with_human_token_works_over_the_socket(daemon):
    with unix_client(daemon.socket_path) as c:
        r = c.get("/v1/whoami", headers=bearer(daemon.human_token))
    assert r.status_code == 200


def test_human_token_over_tcp_is_refused(daemon):
    r = httpx.get(daemon.base_url + "/v1/whoami", headers=bearer(daemon.human_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "human_only"


def test_serve_replaces_a_stale_socket_file(short_home):
    import asyncio

    from covey.config import Config
    from covey.daemon.app import serve

    cfg = Config(home=short_home / "h", port=free_port())
    cfg.home.mkdir(mode=0o700)
    cfg.socket_path.write_text("stale")  # a leftover from a crashed daemon

    async def go():
        ready, stop = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(serve(cfg, ready=ready, stop=stop))
        await asyncio.wait_for(ready.wait(), 10)
        mode = stat.S_IMODE(os.stat(cfg.socket_path).st_mode)
        stop.set()
        await asyncio.wait_for(task, 10)
        return mode

    assert asyncio.run(go()) == 0o600
    assert not cfg.socket_path.exists()


def test_serve_stop_removes_socket(daemon):
    # the fixture teardown stops it; here we only check the socket is there while running
    assert os.path.exists(daemon.socket_path)


def test_serve_refuses_when_another_daemon_answers(daemon):
    import asyncio

    from covey.daemon.app import serve

    cfg = daemon.cfg.__class__(home=daemon.cfg.home, port=free_port())
    with pytest.raises(SystemExit) as exc:
        asyncio.run(serve(cfg))
    assert "already" in str(exc.value)
    assert os.path.exists(
        daemon.socket_path
    )  # the running daemon's socket is untouched


def test_serve_fails_clearly_when_port_taken(short_home):
    import asyncio

    from covey.config import Config
    from covey.daemon.app import serve

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        cfg = Config(home=short_home / "h", port=port)
        with pytest.raises(SystemExit) as exc:
            asyncio.run(serve(cfg))
    msg = str(exc.value)
    assert str(port) in msg
    assert "COVEY_PORT" in msg
    assert "coveyd.log" in msg
    assert not cfg.socket_path.exists()


def test_sweeper_survives_unimplemented_sweep(daemon):
    # registry.sweep / router.expire_due may raise; the daemon must keep answering.
    import time

    time.sleep(0.6)  # a few sweep ticks (sweep_interval_s=0.2)
    with unix_client(daemon.socket_path) as c:
        r = c.get("/v1/whoami", headers=bearer(daemon.human_token))
    assert r.status_code == 200


# ---- covey up / status / down against a real detached daemon -------------------------


@pytest.fixture
def cli_env(short_home, monkeypatch):
    home = short_home / "h"
    monkeypatch.setenv("COVEY_HOME", str(home))
    monkeypatch.setenv("COVEY_PORT", str(free_port()))
    yield home
    # make sure no daemon outlives the test
    pid_file = home / "coveyd.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 9)
        except (ProcessLookupError, ValueError):
            pass


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_status_before_up_then_up_status_down(cli_env):
    res = run("status")
    assert res.exit_code == 1 and "covey up" in res.output

    res = run("up")
    assert res.exit_code == 0, res.output

    res = run("status", "--json")
    assert res.exit_code == 0, res.output
    info = json.loads(res.output)
    assert info["running"] is True
    assert info["pid"] == int((cli_env / "coveyd.pid").read_text())
    assert info["socket"] == str(cli_env / "covey.sock")
    assert info["port"] == int(os.environ["COVEY_PORT"])
    assert info["node_id"]

    assert stat.S_IMODE(os.stat(cli_env / "coveyd.pid").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cli_env / "covey.sock").st_mode) == 0o600

    res = run("down")
    assert res.exit_code == 0, res.output
    assert not (cli_env / "coveyd.pid").exists()
    assert not (cli_env / "covey.sock").exists()

    res = run("status")
    assert res.exit_code == 1


def test_up_twice_reports_already_running(cli_env):
    assert run("up").exit_code == 0
    res = run("up")
    assert res.exit_code == 0
    assert "already running" in res.output
    assert run("down").exit_code == 0


def test_up_with_port_taken_fails_with_fix_and_log_path(cli_env):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        os.environ["COVEY_PORT"] = str(port)
        res = run("up")
    assert res.exit_code != 0
    assert str(port) in res.output
    assert "COVEY_PORT" in res.output
    assert "coveyd.log" in res.output
    assert not (cli_env / "coveyd.pid").exists()


def test_down_with_stale_pidfile_cleans_up(cli_env):
    cli_env.mkdir(mode=0o700)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    (cli_env / "coveyd.pid").write_text(str(proc.pid))  # dead pid
    res = run("down")
    assert res.exit_code == 0
    assert not (cli_env / "coveyd.pid").exists()


def test_down_when_not_running_is_not_an_error(cli_env):
    res = run("down")
    assert res.exit_code == 0
    assert "not running" in res.output


def test_down_never_signals_a_process_that_is_not_coveyd(cli_env):
    """A pidfile left by a crash can name a pid the OS has since given to something else."""
    cli_env.mkdir(mode=0o700)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (cli_env / "coveyd.pid").write_text(str(bystander.pid))
        res = run("down")
        assert bystander.poll() is None, "covey down killed an unrelated process"
        assert res.exit_code == 1
        assert str(bystander.pid) in res.output
    finally:
        bystander.kill()
        bystander.wait()
