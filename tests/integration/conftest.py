"""Integration fixtures: a real daemon running in-process."""

import asyncio
import contextlib
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from covey.config import Config
from covey.daemon.app import serve


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class RunningDaemon:
    """What the `daemon` fixture yields.

    cfg          the Config in use (cfg.home is a short dir under /tmp, cfg.port a free port)
    human_token  contents of the human token; valid only over the unix socket
    join_token   contents of the join token; registers and heartbeats
    socket_path  str path of the unix socket (httpx.HTTPTransport(uds=socket_path))
    base_url     "http://127.0.0.1:<port>" for TCP calls, e.g. httpx.Client(base_url=daemon.base_url)
    """

    cfg: Config
    human_token: str
    join_token: str
    socket_path: str
    base_url: str


@pytest.fixture
def short_home():
    """A fresh COVEY_HOME short enough for a unix socket path."""
    home = Path(tempfile.mkdtemp(prefix="covey-", dir="/tmp"))
    yield home
    shutil.rmtree(home, ignore_errors=True)


@contextlib.contextmanager
def run_daemon(cfg: Config) -> Iterator[RunningDaemon]:
    """A real daemon on `cfg`, served in a background thread until the block exits."""
    ready_flag = threading.Event()
    failure: list[BaseException] = []
    handles: dict = {}

    async def run() -> None:
        ready, stop = asyncio.Event(), asyncio.Event()
        handles["loop"], handles["stop"] = asyncio.get_running_loop(), stop
        task = asyncio.create_task(serve(cfg, ready=ready, stop=stop))
        waiter = asyncio.create_task(ready.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            failure.append(task.exception() or RuntimeError("serve returned early"))
            ready_flag.set()
            return
        ready_flag.set()
        await task

    def target() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:  # noqa: BLE001 (SystemExit too)
            failure.append(exc)
            ready_flag.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    assert ready_flag.wait(15), "daemon did not become ready"
    if failure:
        raise failure[0]
    try:
        yield RunningDaemon(
            cfg=cfg,
            human_token=cfg.token_path.read_text().strip(),
            join_token=cfg.join_token_path.read_text().strip(),
            socket_path=str(cfg.socket_path),
            base_url=f"http://127.0.0.1:{cfg.port}",
        )
    finally:
        handles["loop"].call_soon_threadsafe(handles["stop"].set)
        thread.join(15)
        assert not thread.is_alive(), "daemon did not stop"


@pytest.fixture
def daemon(short_home) -> Iterator[RunningDaemon]:
    cfg = Config(home=short_home / "h", port=free_port(), sweep_interval_s=0.2)
    with run_daemon(cfg) as running:
        yield running
