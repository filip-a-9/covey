"""Who is on the other end of the unix socket, and which agent's process tree they sit in.

The human token is a speed bump for agents, not a sandbox: a call from inside a registered
agent's process tree (a session, or a running headless worker) is refused. A process that
double-forks out of the tree escapes it (accepted; see the trust spec).
"""

import json
import os
import socket
import struct
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from covey.procs import _parent_and_argv0, _start_time, process_cwd

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
            # SOL_LOCAL, LOCAL_PEERPID. No buffer length: with one, Python returns raw bytes.
            return sock.getsockopt(0, 0x002)
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


def own_ancestry(table: Table = _parent_and_argv0) -> set[int]:
    """This process's pid and its ancestors. A test daemon running inside pytest passes them as
    `serve(ignore_pids=...)`, so an outer Claude session running the suite is not a session
    root. coveyd itself ignores nothing: `launch.ensure_daemon` detaches it from its caller."""
    return {pid for pid, _ in ancestors(os.getpid(), table)}


def session_id(pid: int) -> int | None:
    """The pid's session id (the setsid boundary), or None where it cannot be read."""
    try:
        return os.getsid(pid)  # POSIX: Linux and macOS
    except OSError:
        return None


def roots_from_session_files(
    sessions: Path, node_id: str, skip: Collection[int] = ()
) -> dict[str, Root]:
    """Roots of live sessions, from the shims' `claude-<pid>.json` files (slug, pid and
    start time; the token in them is never read). A recycled pid does not match."""
    roots: dict[str, Root] = {}
    for path in sessions.glob("claude-*.json"):
        try:
            data = json.loads(path.read_text())
            pid, start = int(data["claude_pid"]), str(data["claude_start_time"])
            agent_id = f"{data['slug']}@{node_id}"
        except (KeyError, ValueError, TypeError, OSError):
            continue
        if pid not in skip and _start_time(pid) == start:
            roots[agent_id] = (pid, start)
    return roots


def peer_chain(pid: int, table: Table = _parent_and_argv0) -> list[int] | None:
    """The peer and its ancestors, or None when the peer's own entry is gone (it exited):
    a walk that does not start with the peer proves nothing about who called."""
    chain = [p for p, _ in ancestors(pid, table)]
    return chain if chain and chain[0] == pid else None


def session_root(
    peer: int,
    table: Table = _parent_and_argv0,
    start_time: StartTime = _start_time,
    skip: Collection[int] = (),
    session: Callable[[int], int | None] = session_id,
) -> Root | None:
    """The nearest `claude` above `peer`, as (pid, start_time). Without one, the oldest
    ancestor in the peer's own session (setsid boundary), or `peer` where that is unknown.
    Processes in `skip` (the daemon's own ancestry) are never chosen."""
    chain = [(pid, argv0) for pid, argv0 in ancestors(peer, table) if pid not in skip]
    if not chain or chain[0][0] != peer:
        return None
    pick = next(
        (pid for pid, argv0 in chain if os.path.basename(argv0) == "claude"), None
    )
    if pick is None:
        pick = peer
        sid = session(peer)
        for pid, _ in chain:
            if sid is None or session(pid) != sid:
                break
            pick = pid
    start = start_time(pick)
    return None if start is None else (pick, start)


def merge_roots(*sources: Mapping[str, Root]) -> dict[str, list[Root]]:
    """Every root of every agent: a worker's root must not hide the same agent's session."""
    merged: dict[str, list[Root]] = {}
    for source in sources:
        for agent_id, root in source.items():
            merged.setdefault(agent_id, []).append(root)
    return merged


def agents_in_chain(
    chain: Iterable[int],
    roots: Mapping[str, Sequence[Root]],
    start_time: StartTime = _start_time,
    skip: Collection[int] = (),
) -> list[str]:
    """Every agent_id with a root (pid and start time) in `chain`, nearest first."""
    by_pid = {
        pid: (agent_id, start)
        for agent_id, agent_roots in roots.items()
        for pid, start in agent_roots
    }
    found: list[str] = []
    for pid in chain:
        if pid in skip or pid not in by_pid:
            continue
        agent_id, start = by_pid[pid]
        if start_time(pid) == start and agent_id not in found:
            found.append(agent_id)
    return found


def find_agent(
    chain: Iterable[int],
    roots: Mapping[str, Sequence[Root]],
    start_time: StartTime = _start_time,
    skip: Collection[int] = (),
) -> str | None:
    """The agent_id with a root (pid and start time) in `chain`, the peer and its ancestors."""
    found = agents_in_chain(chain, roots, start_time, skip)
    return found[0] if found else None


@dataclass(frozen=True)
class Origin:
    """Where a unix-socket register call came from: the agents whose process trees the peer
    sits in, and the peer's working directory (None when unreadable)."""

    agents: frozenset[str]
    cwd: str | None


def origin_of(
    pid: int | None,
    chain: Sequence[int] | None,
    start: str | None,
    roots: Mapping[str, Sequence[Root]],
    skip: Collection[int] = (),
) -> Origin | None:
    """The Origin of a peer, or None if it cannot be identified: no pid, or it exited."""
    if pid is None or chain is None or _start_time(pid) != start:
        return None
    agents = frozenset(agents_in_chain(chain, roots, skip=skip))
    return Origin(agents, process_cwd(pid))


def peer_protocol(base: type) -> type:
    """A uvicorn protocol class that puts the peer pid in every request scope as
    `covey_peer_pid`. Both h11 and httptools read `self.app` when a request starts."""

    class Peer(base):  # type: ignore[misc, valid-type]
        def connection_made(self, transport: Any) -> None:
            super().connection_made(transport)
            pid = peer_pid(transport.get_extra_info("socket"))
            # Resolved now, while the peer is alive: a peer already gone has no chain.
            chain = peer_chain(pid) if pid is not None else None
            start = _start_time(pid) if chain else None
            if chain is None or start is None:
                pid, chain = None, None
            inner = self.app

            async def app(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] in ("http", "websocket"):
                    scope = {
                        **scope,
                        "covey_peer_pid": pid,
                        "covey_peer_chain": chain,
                        "covey_peer_start": start,
                    }
                await inner(scope, receive, send)

            self.app = app

    return Peer
