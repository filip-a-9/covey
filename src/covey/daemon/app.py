"""coveyd: the FastAPI app, auth, and the two-transport server.

One `Daemon` object (in `app.state.daemon`) holds the config, the node, the single SQLite
connection, the token hashes and the long-poll condition. All handlers are `async def` and
call the synchronous store modules directly on the event loop thread, so the one connection
is never used from two threads at once.

Auth: `caller()` resolves the bearer token to a `Caller` of kind human, join or agent.
A human-token call that arrived over TCP is refused with 403 `human_only`, whatever the
endpoint. Each uvicorn server wraps the app with `tag_transport()` so a handler can tell
which door a request came in by.

Long-poll: after every mutating call (and every sweep tick) the app calls
`await daemon.notify()`; waiters loop on `daemon.wait_for(predicate, timeout)`.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from covey import API_VERSION, frames
from covey import config as config_module
from covey.config import Config, ConfigError
from covey.daemon import (
    events,
    grants,
    notify,
    peer,
    registry,
    router,
    store,
    wake,
    waker,
)
from covey.daemon.errors import CoveyError
from covey.text import safe

Transport = Literal["unix", "tcp"]


@dataclass
class Caller:
    kind: Literal["human", "join", "agent"]
    agent_id: str | None  # human@<node> for the human, None for the join token
    slug: str | None
    worker_request: str | None = None  # set for a headless worker's token
    worker_created: set[str] | None = (
        None  # requests that worker asked, shared with its token
    )


@dataclass(frozen=True)
class WorkerAuth:
    """A spawned worker's token, held only in memory: it acts as `agent_id` for one request."""

    agent_id: str
    request_id: str
    created: set[str] = field(default_factory=set)  # sub-requests it asked


class Daemon:
    def __init__(self, cfg: Config, ignore_pids: frozenset[int] = frozenset()):
        self.cfg = cfg
        self.node = store.init_home(cfg)
        # One connection per thread: FastAPI runs sync handlers in a threadpool, and threads
        # sharing a connection share its transaction (and Python 3.13 refuses the misuse).
        self._db_local = threading.local()
        self._db_conns: list[sqlite3.Connection] = []
        self._db_lock = threading.Lock()
        self.human_id = f"human@{self.node.node_id}"
        self.human_hash = store.hash_token(cfg.token_path.read_text().strip())
        self.join_hash = store.hash_token(cfg.join_token_path.read_text().strip())
        self.workers: dict[str, WorkerAuth] = {}  # token hash -> worker
        # Workers die with the daemon, so a `working` request left from before is lost.
        waker.recover(self.conn, self.node.node_id, cfg.workers_path)
        self.waker = waker.Waker(self)
        # agent_id -> (pid, start_time) of the process tree its session lives in. In memory,
        # re-derived at startup from the shims' session files.
        self.session_pids: dict[str, peer.Root] = peer.roots_from_session_files(
            cfg.home / "sessions", self.node.node_id, ignore_pids
        )
        # agent_id -> path of its Claude Code messaging socket (idle wake). In memory only,
        # relearned on the next heartbeat. Never the socket's token.
        self.wake_sockets: dict[str, str] = {}
        self.wake_tasks: set[asyncio.Task] = set()
        # request_id -> open `?wait=` long-polls on it. In memory only; a reply posts to its
        # asker only when this is 0.
        self.waiting: dict[str, int] = {}
        # Never a session root and never matched. Empty in production; a test daemon running
        # inside pytest passes pytest's own ancestry, which may include a real `claude`.
        self.ignore_pids = ignore_pids
        notify.configure(cfg.notify)
        self.human_watchers = 0  # open human event streams and approval long-polls
        self._changed: asyncio.Condition | None = None

    @property
    def changed(self) -> asyncio.Condition:
        if self._changed is None:
            self._changed = asyncio.Condition()
        return self._changed

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for(self, predicate: Callable[[], Any], timeout: float) -> Any:
        """Return predicate()'s first truthy value, or its last value after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        value = predicate()
        while not value:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            async with self.changed:
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except TimeoutError:
                    pass
            value = predicate()
        return value

    @contextlib.contextmanager
    def watching(self) -> Iterator[None]:
        """Count a human watcher (event stream or approvals long-poll) while the block runs."""
        self.human_watchers += 1
        try:
            yield
        finally:
            self.human_watchers -= 1

    @contextlib.contextmanager
    def waiting_on(self, request_id: str) -> Iterator[None]:
        """Count a `?wait=` long-poll on `request_id` while the block runs. The `finally`
        also runs when the poll is cancelled by a client disconnect."""
        self.waiting[request_id] = self.waiting.get(request_id, 0) + 1
        try:
            yield
        finally:
            self.waiting[request_id] -= 1
            if self.waiting[request_id] <= 0:
                del self.waiting[request_id]

    def mint_worker_token(self, agent_id: str, request_id: str) -> str:
        """A fresh token for a worker serving `request_id` as `agent_id`; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        self.workers[store.hash_token(token)] = WorkerAuth(agent_id, request_id)
        return token

    def revoke_worker_token(self, token: str) -> None:
        self.workers.pop(store.hash_token(token), None)

    def note_session(
        self, agent_id: str, request: Request, replace: bool = False
    ) -> None:
        """Remember where this agent's session runs, from a unix-socket call's peer pid."""
        pid = request.scope.get("covey_peer_pid")
        root = None
        if transport_of(request) == "unix" and pid is not None:
            root = peer.session_root(pid, skip=self.ignore_pids)
        if root is None:
            if (
                replace
            ):  # a takeover from somewhere unreadable must not keep the old pid
                self.session_pids.pop(agent_id, None)
        elif replace or agent_id not in self.session_pids:
            # Only register and session attach set the root; a heartbeat keeps the first one.
            self.session_pids[agent_id] = root

    def note_wake_socket(self, agent_id: str, path: str | None) -> None:
        """Remember an agent's wake socket if it is one Claude Code could have made: a
        `*.sock` directly in the session socket dir, not a symlink. The daemon writes to it,
        so any other path an agent names is ignored."""
        if not path or not self._is_session_socket(path):
            return
        known = self.wake_sockets.get(agent_id)
        self.wake_sockets[agent_id] = path
        if path != known:  # e.g. the first heartbeat after a restart: post what waited
            for request in router.never_offered(self.conn, agent_id):
                self.wake(request)

    def _is_session_socket(self, path: str) -> bool:
        if not path.endswith(".sock") or not os.path.isabs(path):
            return False
        if os.path.islink(path):
            return False
        try:
            parent = os.path.realpath(os.path.dirname(path))
            return parent == os.path.realpath(self.cfg.wake_socket_dir)
        except (OSError, ValueError):
            return False

    def wake(self, request: dict[str, Any]) -> None:
        """Idle wake: if this queued request's target is a live session with a wake socket,
        stamp it and post one message in the background. Event-driven only (never from the
        sweep), so a released request is not posted again."""
        cfg = self.cfg
        agent_id = request["to_agent"]
        path = self.wake_sockets.get(agent_id)
        if not (cfg.idle_wake and path and request["status"] == "queued"):
            return
        if router._agent_by_id(self.conn, agent_id)["liveness"] != "live":
            return
        rid = request["request_id"]
        if not router.wake_claim(self.conn, rid):
            return
        actor = store.daemon_actor(self.node.node_id)
        store.audit(self.conn, actor, "request.wake_posted", rid, {"agent": agent_id})
        text = frames.wake_message(
            request["from_agent"].partition("@")[0], request["capability"], rid
        )
        task = asyncio.get_running_loop().create_task(
            self._post_wake(agent_id, path, rid, text)
        )
        self.wake_tasks.add(task)
        task.add_done_callback(self.wake_tasks.discard)

    async def _post_wake(self, agent_id: str, path: str, rid: str, text: str) -> None:
        if not await self._post(agent_id, path, text):
            router.release_wake_claim(self.conn, rid)
            await self.notify()

    async def _post_reply(self, asker: str, path: str, rid: str, text: str) -> None:
        if await self._post(asker, path, text):
            actor = store.daemon_actor(self.node.node_id)
            store.audit(self.conn, actor, "request.reply_posted", rid, {"agent": asker})
            await self.notify()

    async def _post(self, agent_id: str, path: str, text: str) -> bool:
        """Post `text` to a wake socket; on failure drop the socket and return False."""
        try:
            await wake.post(path, text)
        except OSError as exc:
            logger.info("wake post to %s failed: %s", agent_id, safe(exc))
            if self.wake_sockets.get(agent_id) == path:
                del self.wake_sockets[agent_id]
            return False
        return True

    def wake_asker(self, request: dict[str, Any]) -> None:
        """Idle wake for a reply: tell a live asker with a wake socket, in the background,
        that a reply arrived, unless something is already waiting on it. No claim and no
        fallback: a lost post costs no more than not posting."""
        cfg = self.cfg
        rid = request["request_id"]
        asker = request["from_agent"]
        path = self.wake_sockets.get(asker)
        if not (cfg.idle_wake and path and self.waiting.get(rid, 0) == 0):
            return
        try:
            if router._agent_by_id(self.conn, asker)["liveness"] != "live":
                return
        except CoveyError:
            return
        text = frames.reply_message(
            request["to_agent"].partition("@")[0], rid, request["status"]
        )
        task = asyncio.get_running_loop().create_task(
            self._post_reply(asker, path, rid, text)
        )
        self.wake_tasks.add(task)
        task.add_done_callback(self.wake_tasks.discard)

    async def shutdown_wakes(self) -> None:
        for task in list(self.wake_tasks):
            task.cancel()
        await asyncio.gather(*self.wake_tasks, return_exceptions=True)

    def agent_roots(self) -> dict[str, list[peer.Root]]:
        return peer.merge_roots(self.session_pids, self.waker.pids)

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection to the database, opened on first use."""
        conn = getattr(self._db_local, "conn", None)
        if conn is None:
            conn = store.connect(self.cfg.db_path)
            self._db_local.conn = conn
            with self._db_lock:
                self._db_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._db_lock:
            for conn in self._db_conns:
                conn.close()
            self._db_conns.clear()


def error_body(code: str, message: str, next_step: str) -> dict[str, Any]:
    return {
        "covey": API_VERSION,
        "error": {"code": code, "message": message, "next_step": next_step},
    }


def transport_of(request: Request) -> Transport:
    return request.scope.get("covey_transport", "tcp")


def tag_transport(app: Any, transport: Transport) -> Callable:
    """ASGI wrapper that records which transport a request arrived on."""

    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "covey_transport": transport}
        await app(scope, receive, send)

    return wrapped


class Limits:
    """ASGI wrapper, outside auth: refuses a foreign Host on TCP and bodies over the cap.

    Never buffers: a declared Content-Length is checked up front, and a chunked body is
    cut off once it passes the cap.
    """

    def __init__(self, app: Any, cfg: Config) -> None:
        self.app = app
        self.max_body = cfg.max_body_bytes
        self.port = cfg.port
        self.hosts = {f"{h}:{cfg.port}" for h in ("127.0.0.1", "localhost", "[::1]")}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if scope.get("covey_transport", "tcp") == "tcp":
            host = headers.get(b"host", b"").decode("latin-1").lower()
            if host not in self.hosts:
                await self._refuse(
                    send,
                    403,
                    "bad_host",
                    f"Host {host!r} is not this daemon's address.",
                    f"Call http://127.0.0.1:{self.port} or http://localhost:{self.port}.",
                )
                return
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_body:
            await self._refuse(send, 413, *self._too_large())
            return
        seen = 0
        refused = False

        async def counted():
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request" and not refused:
                seen += len(message.get("body", b""))
                if seen > self.max_body:
                    # Answer now and tell the app the client is gone: nothing more is read.
                    refused = True
                    await self._refuse(send, 413, *self._too_large())
                    return {"type": "http.disconnect"}
            return message

        async def tracked(message):
            if not refused:
                await send(message)

        await self.app(scope, counted, tracked)

    def _too_large(self) -> tuple[str, str, str]:
        return (
            "body_too_large",
            f"The request body is over {self.max_body} bytes.",
            "Send less: keep task, context and result short and put big content in a file.",
        )

    async def _refuse(self, send, status, code, message, next_step) -> None:
        body = json.dumps(error_body(code, message, next_step)).encode()
        start = {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
        await send(start)
        await send({"type": "http.response.body", "body": body})


def get_daemon(request: Request) -> Daemon:
    return request.app.state.daemon


def refuse_if_in_agent_tree(request: Request, daemon: Daemon) -> None:
    """Fail closed: the human token over the unix socket must come from outside every
    registered agent's process tree, and the peer must be identifiable."""
    pid = request.scope.get("covey_peer_pid")
    # The peer must still be the process that connected: an exited one proves nothing.
    if pid is None or peer._start_time(pid) != request.scope.get("covey_peer_start"):
        raise CoveyError(
            403,
            "human_only",
            "Cannot tell which process made this call, so the human token is refused.",
            "Run `covey doctor` from your own terminal.",
        )
    agent_id = peer.find_agent(
        request.scope["covey_peer_chain"], daemon.agent_roots(), skip=daemon.ignore_pids
    )
    if agent_id is not None:
        raise CoveyError(
            403,
            "human_only",
            f"This call came from inside the process tree of agent {agent_id}.",
            "Run the `covey` CLI from your own terminal, outside any agent session.",
        )


def register_origin(request: Request, daemon: Daemon) -> peer.Origin | None:
    """Where a register call came from; None over TCP or when the peer is unreadable."""
    if transport_of(request) != "unix":
        return None
    scope = request.scope
    return peer.origin_of(
        scope.get("covey_peer_pid"),
        scope.get("covey_peer_chain"),
        scope.get("covey_peer_start"),
        daemon.agent_roots(),
        daemon.ignore_pids,
    )


def caller(request: Request) -> Caller:
    """Resolve the bearer token. 401 if missing or unknown; 403 for the human token over TCP."""
    daemon = get_daemon(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise CoveyError(
            401,
            "unauthorized",
            "This call needs a bearer token.",
            "Send `Authorization: Bearer <token>`: the agent token from register, the join "
            "token in ~/.covey/join-token, or use the `covey` CLI.",
        )
    digest = store.hash_token(token.strip())
    if hmac.compare_digest(digest, daemon.human_hash):
        if transport_of(request) != "unix":
            raise CoveyError(
                403,
                "human_only",
                "The human token is accepted only over the unix socket.",
                "Run the `covey` CLI from your own terminal.",
            )
        refuse_if_in_agent_tree(request, daemon)
        return Caller("human", daemon.human_id, "human")
    if hmac.compare_digest(digest, daemon.join_hash):
        return Caller("join", None, None)
    row = registry.agent_by_token_hash(daemon.conn, digest)
    if row is not None and hmac.compare_digest(digest, row["token_hash"] or ""):
        return Caller("agent", row["agent_id"], row["slug"])
    worker = daemon.workers.get(digest)
    if worker is not None:
        slug = worker.agent_id.partition("@")[0]
        return Caller("agent", worker.agent_id, slug, worker.request_id, worker.created)
    raise CoveyError(
        401,
        "unauthorized",
        "Unknown token. Agent tokens are replaced on every register.",
        "Register again with the join token to get a fresh agent token.",
    )


def not_worker(who: Caller) -> None:
    """Liveness and sessions belong to a real session; a headless worker must not touch them."""
    if who.worker_request is not None:
        raise CoveyError(
            403,
            "wrong_token",
            "A headless worker cannot heartbeat or attach a session.",
            "Run `covey mcp --worker`, which never does either.",
        )


def worker_only_own(who: Caller, request_id: str) -> None:
    """A worker may touch only the request it serves and the ones it asked."""
    if (
        who.worker_request is not None
        and request_id != who.worker_request
        and request_id not in (who.worker_created or ())
    ):
        raise CoveyError(
            403,
            "not_worker",
            "A headless worker can use only its own request and the ones it asked.",
            "Reply to your own request, or ask with `covey_ask`.",
        )


def refuse_worker(who: Caller) -> None:
    """Inbox, stop-claim and card edits belong to the agent's real session."""
    if who.worker_request is not None:
        raise CoveyError(
            403,
            "not_worker",
            "A headless worker cannot read the inbox or edit the card.",
            "Reply to your own request with `covey_reply`; the daemon already gave you the task.",
        )


def require(*kinds: str) -> Callable[..., Caller]:
    """Dependency factory: the caller must be one of `kinds`."""

    def dependency(who: Annotated[Caller, Depends(caller)]) -> Caller:
        if who.kind not in kinds:
            if kinds == ("human",):
                raise CoveyError(
                    403,
                    "human_only",
                    "Only the human can do this.",
                    "Run the `covey` CLI from your own terminal.",
                )
            raise CoveyError(
                403,
                "wrong_token",
                f"A {who.kind} token cannot make this call.",
                "The join token only registers and heartbeats; register to get an agent token.",
            )
        return who

    return dependency


SLUG = r"^[a-z0-9][a-z0-9_-]{0,62}$"
ULID = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
# No C0/C1 controls (pydantic's regex engine: `$` is end of text only, never before a newline).
NO_CONTROLS = r"^[^\x00-\x1f\x7f-\x9f]*$"
GRANT_SCOPE = Literal["once", "session", "always", "deny"]


def utc_text(value: datetime | None) -> str | None:
    """A timezone-aware datetime as store text: UTC, milliseconds, trailing Z."""
    if value is None:
        return None
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class Body(BaseModel):
    """Base for request bodies: unknown fields are a 400."""

    model_config = ConfigDict(extra="forbid")


class RegisterBody(Body):
    slug: str = Field(pattern=SLUG)
    kind: str = Field(min_length=1, max_length=64, pattern=NO_CONTROLS)
    delivery: Literal["hooks", "webhook", "poll", "spawn"]
    workdir: str | None = Field(default=None, pattern=NO_CONTROLS)
    summary: str = Field(default="", max_length=1000)
    tags: list[str] = []
    capabilities: list[dict[str, Any]] = []
    instructions: str | None = Field(default=None, max_length=4000)
    webhook_url: str | None = None
    wakeable: bool | None = None  # accepted but ignored: only the human token sets it
    session_id: str | None = None
    wake_socket: str | None = Field(default=None, max_length=200, pattern=NO_CONTROLS)


class CreateAgentBody(Body):
    slug: str = Field(pattern=SLUG)
    kind: str = Field(
        default="claude-code", min_length=1, max_length=64, pattern=NO_CONTROLS
    )
    workdir: str = Field(min_length=1, pattern=NO_CONTROLS)
    summary: str = Field(default="", max_length=1000)
    tags: list[str] = []
    capabilities: list[dict[str, Any]] = []
    instructions: str | None = Field(default=None, max_length=4000)
    wakeable: bool = False
    worker: dict[str, Any] | None = None  # validated against card.json by the registry


class DescribeBody(Body):
    summary: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = None
    capabilities: list[dict[str, Any]] | None = None
    instructions: str | None = Field(default=None, max_length=4000)


class SessionBody(Body):
    session_id: str = Field(min_length=1, max_length=200)
    wake_socket: str | None = Field(default=None, max_length=200, pattern=NO_CONTROLS)


class HeartbeatBody(Body):
    wake_socket: str | None = Field(default=None, max_length=200, pattern=NO_CONTROLS)


class SessionEndBody(Body):
    reason: str = Field(max_length=200)


class AskBody(Body):
    to: str
    task: str = Field(min_length=1, max_length=65536)
    capability: str
    context: str | None = Field(default=None, max_length=65536)
    thread_id: str | None = Field(default=None, pattern=ULID)
    parent_id: str | None = Field(default=None, pattern=ULID)
    timeout_s: int | None = Field(default=None, ge=1, le=86400)


class ReplyBody(Body):
    result: str = Field(max_length=65536)
    status: str
    artifacts: list[dict[str, Any]] = Field(default=[], max_length=32)


class GrantBody(Body):
    requester: str
    target: str
    capability: str
    scope: GRANT_SCOPE
    expires_at: str | None = None

    @field_validator("expires_at")
    @classmethod
    def iso_with_timezone(cls, value: str | None) -> str | None:
        """An ISO-8601 date-time with a timezone, stored as UTC text."""
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "must be an ISO-8601 date-time, e.g. 2030-01-01T00:00:00Z"
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError("needs a timezone, e.g. a trailing Z")
        return utc_text(parsed)


class ApprovalBody(Body):
    scope: GRANT_SCOPE


def versioned(**parts: Any) -> dict[str, Any]:
    return {"covey": API_VERSION, **parts}


def forbidden(message: str, next_step: str) -> CoveyError:
    return CoveyError(403, "forbidden", message, next_step)


def create_app(cfg: Config, ignore_pids: frozenset[int] = frozenset()) -> FastAPI:
    app = FastAPI(
        title="coveyd",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.daemon = Daemon(cfg, ignore_pids)
    app.add_middleware(Limits, cfg=cfg)

    @app.exception_handler(CoveyError)
    async def covey_error(_: Request, exc: CoveyError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.next_step), exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return JSONResponse(
            error_body(
                "bad_request",
                f"Invalid request: {where}: {first.get('msg', 'invalid')}",
                "Fix the request to match src/covey/schema/api.yaml.",
            ),
            400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(
            error_body(
                code, str(exc.detail), "Check the method and path against api.yaml."
            ),
            exc.status_code,
        )

    # Endpoints: one handler per api.yaml operation except /v1/events (SSE, TUI slice).

    @app.get("/v1/whoami")
    async def whoami(
        who: Annotated[Caller, Depends(require("human", "agent"))],
    ) -> dict:
        daemon = app.state.daemon
        me = registry.whoami(daemon.conn, who.agent_id, daemon.node.node_id)
        return {"covey": API_VERSION, **me}

    def own_slug(who: Caller, slug: str) -> None:
        """An agent token may act only on its own slug; the human may act on any."""
        if who.kind == "agent" and who.slug != slug:
            raise forbidden(
                f"Your token belongs to {who.slug!r}, not {slug!r}.",
                "Use your own slug, or ask the human to run the `covey` CLI.",
            )

    def own_request(who: Caller, request_id: str) -> dict[str, Any]:
        worker_only_own(who, request_id)
        req = router.get_request(app.state.daemon.conn, request_id)
        if who.kind == "agent" and not router.is_participant(req, who.agent_id):
            raise forbidden(
                "You did not send or receive that request.",
                "Read only requests you took part in (`covey_log`).",
            )
        return req

    @app.post("/v1/agents/register")
    async def register_agent(
        request: Request,
        body: RegisterBody,
        who: Annotated[Caller, Depends(require("join"))],
    ) -> dict:
        daemon = app.state.daemon
        card, token = registry.register(
            daemon.conn,
            daemon.node.node_id,
            body.model_dump(
                exclude_none=True,
                exclude_unset=True,
                exclude={"wake_socket", "wakeable"},
            ),
            register_origin(request, daemon),
        )
        daemon.note_session(card["agent_id"], request, replace=True)
        daemon.note_wake_socket(card["agent_id"], body.wake_socket)
        await daemon.notify()
        return versioned(card=card, agent_token=token)

    @app.post("/v1/agents", status_code=201)
    async def create_agent(
        body: CreateAgentBody, who: Annotated[Caller, Depends(require("human"))]
    ) -> dict:
        daemon = app.state.daemon
        # exclude_unset: a field the caller left out must not arrive as its model default,
        # or an update would reset it.
        fields = body.model_dump(exclude_none=True, exclude_unset=True)
        registry.check_workdir(fields.get("workdir"), daemon.cfg.home)
        card = registry.create_agent(daemon.conn, daemon.node.node_id, fields)
        await daemon.notify()
        return versioned(card=card)

    @app.post("/v1/agents/{slug}/heartbeat")
    async def heartbeat_agent(
        request: Request,
        slug: Annotated[str, Path(pattern=SLUG)],
        who: Annotated[Caller, Depends(require("agent", "human"))],
        body: HeartbeatBody | None = None,
    ) -> dict:
        daemon = app.state.daemon
        not_worker(who)
        own_slug(who, slug)
        result = registry.heartbeat(daemon.conn, slug)
        if who.kind != "human":
            agent_id = f"{slug}@{daemon.node.node_id}"
            daemon.note_session(agent_id, request)
            daemon.note_wake_socket(agent_id, body.wake_socket if body else None)
        await daemon.notify()
        return versioned(**result)

    @app.get("/v1/agents")
    async def find_agents(
        who: Annotated[Caller, Depends(require("human", "agent"))],
        need: Annotated[str | None, Query(max_length=1000)] = None,
        capability: str | None = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 5,
    ) -> dict:
        # `limit` caps ranked results only; a plain list returns every non-gone agent.
        found = registry.find(
            app.state.daemon.conn, need, capability, limit if need else None
        )
        return versioned(matches=found)

    @app.put("/v1/agents/{slug}/card")
    async def describe_agent(
        slug: Annotated[str, Path(pattern=SLUG)],
        body: DescribeBody,
        who: Annotated[Caller, Depends(require("human", "agent"))],
    ) -> dict:
        daemon = app.state.daemon
        refuse_worker(who)
        own_slug(who, slug)
        fields = body.model_dump(exclude_none=True)
        if not fields:
            raise CoveyError(
                400,
                "bad_request",
                "The body has no fields to update.",
                "Send at least one of summary, tags, capabilities, instructions.",
            )
        card = registry.describe(
            daemon.conn, slug, fields, by_agent=who.kind == "agent"
        )
        await daemon.notify()
        return versioned(card=card)

    @app.get("/v1/agents/{slug}/inbox")
    async def get_inbox(
        slug: Annotated[str, Path(pattern=SLUG)],
        who: Annotated[Caller, Depends(require("human", "agent"))],
        status: Annotated[
            Literal[
                "queued",
                "awaiting_grant",
                "delivered",
                "working",
                "needs_input",
                "done",
                "failed",
                "refused",
                "expired",
            ]
            | None,
            Query(),
        ] = None,
        peek: bool = False,
    ) -> dict:
        daemon = app.state.daemon
        refuse_worker(who)
        own_slug(who, slug)
        agent = registry.get_agent(daemon.conn, slug)
        # Only the target's own read delivers; the human looking at it is just peeking.
        reader_is_target = who.agent_id == agent["agent_id"] and not peek
        requests = router.inbox(
            daemon.conn, agent["agent_id"], status, reader_is_target
        )
        await daemon.notify()
        return versioned(requests=requests)

    @app.post("/v1/agents/{slug}/inbox/stop-claim")
    async def claim_inbox_for_stop(
        slug: Annotated[str, Path(pattern=SLUG)],
        who: Annotated[Caller, Depends(require("agent"))],
    ) -> dict:
        daemon = app.state.daemon
        refuse_worker(who)
        own_slug(who, slug)
        agent = registry.get_agent(daemon.conn, slug)
        requests = router.stop_claim(daemon.conn, agent["agent_id"])
        await daemon.notify()
        return versioned(requests=requests)

    @app.put("/v1/agents/{slug}/session")
    async def put_agent_session(
        slug: Annotated[str, Path(pattern=SLUG)],
        body: SessionBody,
        request: Request,
        who: Annotated[Caller, Depends(require("agent"))],
    ) -> dict:
        daemon = app.state.daemon
        not_worker(who)
        own_slug(who, slug)
        result = registry.set_session(daemon.conn, slug, body.session_id)
        daemon.note_session(who.agent_id, request, replace=True)
        daemon.note_wake_socket(who.agent_id, body.wake_socket)
        await daemon.notify()
        return versioned(**result)

    @app.delete("/v1/agents/{slug}/session")
    async def end_agent_session(
        slug: Annotated[str, Path(pattern=SLUG)],
        body: SessionEndBody,
        who: Annotated[Caller, Depends(require("agent"))],
    ) -> dict:
        daemon = app.state.daemon
        not_worker(who)
        own_slug(who, slug)
        result = registry.end_session(daemon.conn, slug, body.reason)
        daemon.session_pids.pop(who.agent_id, None)
        daemon.wake_sockets.pop(who.agent_id, None)
        await daemon.notify()
        return versioned(**result)

    @app.post("/v1/requests", status_code=201)
    async def create_request(
        body: AskBody, who: Annotated[Caller, Depends(require("human", "agent"))]
    ) -> dict:
        daemon = app.state.daemon
        try:
            req = router.create_request(
                daemon.conn,
                who.agent_id,
                body.to,
                body.capability,
                body.task,
                context=body.context,
                thread_id=body.thread_id,
                # A worker's asks always continue its own request, never another.
                parent_id=who.worker_request or body.parent_id,
                timeout_s=body.timeout_s,
                max_hops=daemon.cfg.max_hops,
                max_open_gated=daemon.cfg.max_open_gated,
            )
        except CoveyError as exc:
            # Always, even with a watcher: the refused request is never stored, so no TUI shows it.
            if exc.code == "target_unreachable":
                notify.human(
                    f"{who.slug or 'the human'} asked {body.to}, but {body.to} has no session"
                    " and is not wakeable. Start a Claude Code session in its repo, or run"
                    f" `covey agent add {body.to} --workdir <its repo> --wakeable`."
                )
            raise
        if who.worker_created is not None:
            who.worker_created.add(req["request_id"])
        daemon.wake(req)
        await daemon.notify()
        daemon.waker.kick()
        if req["status"] == "awaiting_grant" and daemon.human_watchers == 0:
            rid = req["request_id"]
            notify.human(
                f"{req['from_agent']} wants {req['capability']} on {req['to_agent']}."
                f" Run `covey approve {rid}`."
            )
        return versioned(request=req)

    @app.get("/v1/requests/{request_id}")
    async def get_request(
        request_id: Annotated[str, Path(pattern=ULID)],
        who: Annotated[Caller, Depends(require("human", "agent"))],
        wait: Annotated[int, Query(ge=0, le=600)] = 0,
    ) -> dict:
        daemon = app.state.daemon
        own_request(who, request_id)

        def finished() -> dict[str, Any] | None:
            req = router.get_request(daemon.conn, request_id)
            # needs_input is not final, but the asker has to hear the question.
            returnable = (*router.FINAL_STATUSES, "needs_input")
            return req if req["status"] in returnable else None

        if wait:
            with daemon.waiting_on(request_id):
                req = await daemon.wait_for(finished, wait)
        else:
            req = None
        return versioned(request=req or router.get_request(daemon.conn, request_id))

    @app.post("/v1/requests/{request_id}/reply")
    async def reply_request(
        request_id: Annotated[str, Path(pattern=ULID)],
        body: ReplyBody,
        who: Annotated[Caller, Depends(require("human", "agent"))],
    ) -> dict:
        daemon = app.state.daemon
        worker_only_own(who, request_id)
        before = router.get_request(daemon.conn, request_id)["status"]
        try:
            req = router.reply(
                daemon.conn,
                request_id,
                who.agent_id,
                body.result,
                body.status,
                body.artifacts,
            )
        finally:
            # A revoked-grant reply fails the request and then raises: wake waiters anyway.
            await daemon.notify()
        if req["status"] != before:  # an identical repeat of a reply posts nothing
            daemon.wake_asker(req)
        return versioned(request=req)

    @app.get("/v1/grants")
    async def list_grants(
        who: Annotated[Caller, Depends(require("human", "agent"))],
        requester: str | None = None,
        target: str | None = None,
    ) -> dict:
        conn = app.state.daemon.conn
        ids = [
            registry.get_agent(conn, s)["agent_id"] if s else None
            for s in (requester, target)
        ]
        involving = who.agent_id if who.kind == "agent" else None
        return versioned(grants=grants.list_grants(conn, ids[0], ids[1], involving))

    @app.post("/v1/grants", status_code=201)
    async def create_grant(
        body: GrantBody, who: Annotated[Caller, Depends(require("human"))]
    ) -> dict:
        daemon = app.state.daemon
        requester = registry.get_agent(daemon.conn, body.requester)
        target = registry.get_agent(daemon.conn, body.target)
        session = target["session_id"] if target["liveness"] == "live" else None
        grant = grants.create_grant(
            daemon.conn,
            requester["agent_id"],
            target["agent_id"],
            body.capability,
            body.scope,
            who.agent_id,
            session_id=session,
            expires_at=body.expires_at,
        )
        await daemon.notify()
        return versioned(grant=grant)

    @app.delete("/v1/grants/{grant_id}")
    async def revoke_grant(
        grant_id: Annotated[str, Path(pattern=ULID)],
        who: Annotated[Caller, Depends(require("human"))],
    ) -> dict:
        daemon = app.state.daemon
        grant = grants.revoke_grant(daemon.conn, grant_id, who.agent_id)
        await daemon.notify()
        return versioned(grant=grant)

    @app.get("/v1/approvals")
    async def list_approvals(
        who: Annotated[Caller, Depends(require("human"))],
        wait: Annotated[int, Query(ge=0, le=600)] = 0,
    ) -> dict:
        daemon = app.state.daemon
        pending = router.pending_approvals(daemon.conn)
        if wait and not pending:
            with daemon.watching():
                pending = await daemon.wait_for(
                    lambda: router.pending_approvals(daemon.conn), wait
                )
        return versioned(requests=pending)

    @app.post("/v1/approvals/{request_id}")
    async def answer_approval(
        request_id: Annotated[str, Path(pattern=ULID)],
        body: ApprovalBody,
        who: Annotated[Caller, Depends(require("human"))],
    ) -> dict:
        daemon = app.state.daemon
        req, grant = router.answer_approval(
            daemon.conn, request_id, body.scope, who.agent_id
        )
        daemon.wake(req)
        await daemon.notify()
        daemon.waker.kick()
        return versioned(request=req, grant=grant)

    @app.get("/v1/log")
    async def get_log(
        who: Annotated[Caller, Depends(require("human", "agent"))],
        thread: Annotated[str | None, Query(pattern=ULID)] = None,
        agent: str | None = None,
        since: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict:
        visible = who.agent_id if who.kind == "agent" else None
        found = router.log(
            app.state.daemon.conn, thread, agent, since, limit, visible_to=visible
        )
        events = [{"covey": API_VERSION, **e} for e in found["events"]]
        return versioned(requests=found["requests"], events=events)

    @app.get("/v1/events")
    async def stream_events(
        who: Annotated[Caller, Depends(require("human"))],
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ) -> StreamingResponse:
        daemon = app.state.daemon
        start = (
            events.current_last_id(daemon) if last_event_id is None else last_event_id
        )

        async def watched() -> AsyncIterator[str]:
            with daemon.watching():
                async for frame in events.stream(daemon, start):
                    yield frame

        return StreamingResponse(
            watched(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return app


logger = logging.getLogger("coveyd")


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signals to `serve()`, since two of them share one process."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _socket_answers(path) -> bool:
    """True if something is accepting connections on the unix socket at `path`."""
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def _bind_unix(path) -> socket.socket:
    """Bind a listening unix socket that is mode 0600 from the moment it exists."""
    old = os.umask(0o077)
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(100)
    sock.setblocking(False)
    return sock


def _bind_tcp(cfg: Config) -> socket.socket:
    sock = socket.socket(socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", cfg.port))
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"coveyd cannot listen on 127.0.0.1:{cfg.port} ({exc.strerror}). "
            f"Set COVEY_PORT to a free port and start it again. Log: {cfg.log_path}"
        ) from exc
    sock.listen(100)
    sock.setblocking(False)
    return sock


def drop_session_pids(daemon: Daemon, agent_ids: list[str]) -> None:
    for agent_id in agent_ids:
        daemon.session_pids.pop(agent_id, None)
        daemon.wake_sockets.pop(agent_id, None)


async def _sweeper(daemon: Daemon) -> None:
    """Every sweep_interval_s: apply the liveness timeout, expire due requests, wake waiters."""
    seen: set[str] = set()
    while True:
        await asyncio.sleep(daemon.cfg.sweep_interval_s)
        now = store.now_iso()
        steps = (
            lambda t=now: drop_session_pids(
                daemon, registry.sweep(daemon.conn, t, daemon.cfg.liveness_timeout_s)
            ),
            lambda t=now: router.expire_due(daemon.conn, t),
            lambda: router.release_wake_claims(
                daemon.conn,
                utc_text(
                    datetime.now(UTC)
                    - timedelta(seconds=daemon.cfg.idle_wake_fallback_s)
                ),
            ),
        )
        steps = (
            *steps,
            daemon.waker.kick,
        )  # also catches sessions that ended with work queued
        for step in steps:
            try:
                step()
            except Exception as exc:
                # Log each distinct failure once, not every tick.
                if repr(exc) not in seen:
                    seen.add(repr(exc))
                    logger.exception("sweep step failed")
        await daemon.notify()


async def serve(
    cfg: Config,
    ready: asyncio.Event | None = None,
    stop: asyncio.Event | None = None,
    ignore_pids: frozenset[int] = frozenset(),
) -> None:
    """Run the app on the unix socket and on 127.0.0.1:PORT until stopped.

    Stops on SIGTERM/SIGINT (main thread only), when `stop` is set, or when cancelled.
    `ready` is set once both servers are listening. Failures to start raise SystemExit
    with a message that says what to do. `ignore_pids` is for test daemons only
    (see `peer.own_ancestry`).
    """
    if _socket_answers(cfg.socket_path):
        raise SystemExit(
            f"A coveyd is already running on {cfg.socket_path}. "
            "Run `covey status`, or `covey down` to stop it first."
        )
    tcp_sock = _bind_tcp(cfg)  # fail on a taken port before touching anything else
    app = None
    unix_sock = None
    socket_identity: tuple[int, int] | None = None
    sweeper: asyncio.Task | None = None
    tasks: list[asyncio.Task] = []
    servers: list[_Server] = []
    try:
        app = create_app(cfg, ignore_pids)
        cfg.socket_path.unlink(missing_ok=True)  # stale: nothing answered above
        unix_sock = _bind_unix(cfg.socket_path)
        socket_identity = _path_identity(cfg.socket_path)
        for sock, transport in ((unix_sock, "unix"), (tcp_sock, "tcp")):
            options: dict[str, Any] = {
                "lifespan": "off",
                "log_level": "warning",
                "access_log": False,
            }
            tagged = tag_transport(app, transport)
            if transport == "unix":
                probe = uvicorn.Config(tagged, **options)
                probe.load()  # resolves the "auto" protocol to a class
                options["http"] = peer.peer_protocol(probe.http_protocol_class)
            conf = uvicorn.Config(tagged, **options)
            server = _Server(conf)
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve(sockets=[sock])))

        loop = asyncio.get_running_loop()
        stopper = stop if stop is not None else asyncio.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stopper.set)
        while not all(s.started for s in servers):
            for task in tasks:
                if task.done():
                    task.result()  # raises the startup failure
                    raise RuntimeError("a server stopped during startup")
            await asyncio.sleep(0.01)

        sweeper = asyncio.create_task(_sweeper(app.state.daemon))
        if ready is not None:
            ready.set()
        stop_wait = asyncio.create_task(stopper.wait())
        try:
            await asyncio.wait([stop_wait, *tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
    finally:
        for server in servers:
            server.should_exit = True
        if sweeper is not None:
            sweeper.cancel()
        results = await asyncio.gather(
            *tasks, *([sweeper] if sweeper else []), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("server task ended with %r", result)
        if app is not None:
            await app.state.daemon.waker.shutdown()
            await app.state.daemon.shutdown_wakes()
            app.state.daemon.close()
        for sock in (unix_sock, tcp_sock):
            if sock is not None:
                sock.close()
        _unlink_if_ours(cfg.socket_path, socket_identity)


def main() -> None:
    """`coveyd`: run the daemon in the foreground."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = config_module.load()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    # Checked before writing the pidfile, which belongs to the running daemon.
    if _socket_answers(cfg.socket_path):
        raise SystemExit(
            f"A coveyd is already running on {cfg.socket_path}. "
            "Run `covey status`, or `covey down` to stop it first."
        )
    cfg.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    asyncio.run(_serve_with_pidfile(cfg))


async def _serve_with_pidfile(cfg: Config) -> None:
    """Serve, owning the pidfile only while this process is the one that bound the sockets.

    Concurrent starts race: the losers fail to bind and must leave the winner's pidfile alone.
    """
    ready = asyncio.Event()
    task = asyncio.create_task(serve(cfg, ready=ready))
    wrote = False
    try:
        ready_wait = asyncio.create_task(ready.wait())
        await asyncio.wait([ready_wait, task], return_when=asyncio.FIRST_COMPLETED)
        ready_wait.cancel()
        if ready.is_set():
            fd = os.open(cfg.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            wrote = True
        await task
    finally:
        if wrote and _pidfile_holds(cfg, os.getpid()):
            cfg.pid_path.unlink(missing_ok=True)


def _path_identity(path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _unlink_if_ours(path, identity: tuple[int, int] | None) -> None:
    """Remove `path` only if it is still the file we bound; a newer daemon may own it now."""
    if identity is not None and _path_identity(path) == identity:
        path.unlink(missing_ok=True)


def _pidfile_holds(cfg: Config, pid: int) -> bool:
    try:
        return int(cfg.pid_path.read_text().strip()) == pid
    except (OSError, ValueError):
        return False
