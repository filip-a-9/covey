"""Who is on the other end of the unix socket, and which agent's process tree they sit in.

The human token is a speed bump for agents, not a sandbox: a call from inside a registered
agent's process tree (a session, or a running headless worker) is refused. A process that
double-forks out of the tree escapes it (accepted; see the trust spec).
"""

import os
import socket
import struct
import sys
from collections.abc import Callable, Collection, Mapping
from typing import Any

from covey.procs import _parent_and_argv0, _start_time

Table = Callable[[int], tuple[int, str] | None]  # pid -> (ppid, argv0)
StartTime = Callable[[int], str | None]
Root = tuple[int, str]  # (pid, start_time)


def peer_pid(sock: socket.socket | None) -> int | None:
    """The pid of the process on the other end of a unix socket, or None if unreadable."""
    if sock is None:
        return None
    try:
        if sys.platform == "linux":
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.unpack("3i", raw)[0]
        if sys.platform == "darwin":
            return sock.getsockopt(0, 0x002, 4)  # SOL_LOCAL, LOCAL_PEERPID
    except OSError:
        return None
    return None


def ancestors(
    pid: int, table: Table = _parent_and_argv0, limit: int = 64
) -> list[tuple[int, str]]:
    """`pid` and its parents up to (not including) pid 1, as (pid, argv0)."""
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(chain) < limit:
        found = table(pid)
        if found is None:
            break
        seen.add(pid)
        chain.append((pid, found[1]))
        pid = found[0]
    return chain


def daemon_ancestry(table: Table = _parent_and_argv0) -> set[int]:
    """coveyd's own pid and its ancestors. Computed per check: coveyd is reparented once
    `covey up` exits."""
    return {pid for pid, _ in ancestors(os.getpid(), table)}


def session_root(
    peer: int,
    table: Table = _parent_and_argv0,
    start_time: StartTime = _start_time,
    skip: Collection[int] = (),
) -> Root | None:
    """The nearest `claude` above `peer` (or `peer` itself without one), as (pid, start_time).
    Processes in `skip` (the daemon's own ancestry) are never chosen."""
    chain = [(pid, argv0) for pid, argv0 in ancestors(peer, table) if pid not in skip]
    pick = next(
        (pid for pid, argv0 in chain if os.path.basename(argv0) == "claude"), None
    )
    if pick is None:
        pick = next((pid for pid, _ in chain if pid == peer), None)
    if pick is None:
        return None
    start = start_time(pick)
    return None if start is None else (pick, start)


def find_agent(
    peer: int,
    roots: Mapping[str, Root],
    table: Table = _parent_and_argv0,
    start_time: StartTime = _start_time,
    skip: Collection[int] = (),
) -> str | None:
    """The agent_id whose root (pid and start time) is `peer` or one of its ancestors."""
    by_pid = {pid: (agent_id, start) for agent_id, (pid, start) in roots.items()}
    for pid, _ in ancestors(peer, table):
        if pid in skip or pid not in by_pid:
            continue
        agent_id, start = by_pid[pid]
        if start_time(pid) == start:
            return agent_id
    return None


def peer_protocol(base: type) -> type:
    """A uvicorn protocol class that puts the peer pid in every request scope as
    `covey_peer_pid`. Both h11 and httptools read `self.app` when a request starts."""

    class Peer(base):  # type: ignore[misc, valid-type]
        def connection_made(self, transport: Any) -> None:
            super().connection_made(transport)
            pid = peer_pid(transport.get_extra_info("socket"))
            inner = self.app

            async def app(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] in ("http", "websocket"):
                    scope = {**scope, "covey_peer_pid": pid}
                await inner(scope, receive, send)

            self.app = app

    return Peer
