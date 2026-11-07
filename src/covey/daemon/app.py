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
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from covey import API_VERSION
from covey import config as config_module
from covey.config import Config, ConfigError
from covey.daemon import events, grants, registry, router, store, waker
from covey.daemon.errors import CoveyError

Transport = Literal["unix", "tcp"]


@dataclass
class Caller:
    kind: Literal["human", "join", "agent"]
    agent_id: str | None  # human@<node> for the human, None for the join token
    slug: str | None
    worker_request: str | None = None  # set for a headless worker's token


@dataclass(frozen=True)
class WorkerAuth:
    """A spawned worker's token, held only in memory: it acts as `agent_id` for one request."""

    agent_id: str
    request_id: str


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.node = store.init_home(cfg)
        self.conn = store.connect(cfg.db_path)
        self.human_id = f"human@{self.node.node_id}"
        self.human_hash = store.hash_token(cfg.token_path.read_text().strip())
        self.join_hash = store.hash_token(cfg.join_token_path.read_text().strip())
        self.workers: dict[str, WorkerAuth] = {}  # token hash -> worker
        # Workers die with the daemon, so a `working` request left from before is lost.
        waker.recover(self.conn, self.node.node_id)
        self.waker = waker.Waker(self)
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

    def mint_worker_token(self, agent_id: str, request_id: str) -> str:
        """A fresh token for a worker serving `request_id` as `agent_id`; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        self.workers[store.hash_token(token)] = WorkerAuth(agent_id, request_id)
        return token

    def revoke_worker_token(self, token: str) -> None:
        self.workers.pop(store.hash_token(token), None)

    def close(self) -> None:
        self.conn.close()


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


def get_daemon(request: Request) -> Daemon:
    return request.app.state.daemon


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
        return Caller("human", daemon.human_id, "human")
    if hmac.compare_digest(digest, daemon.join_hash):
        return Caller("join", None, None)
    row = registry.agent_by_token_hash(daemon.conn, digest)
    if row is not None and hmac.compare_digest(digest, row["token_hash"] or ""):
        return Caller("agent", row["agent_id"], row["slug"])
    worker = daemon.workers.get(digest)
    if worker is not None:
        slug = worker.agent_id.partition("@")[0]
        return Caller("agent", worker.agent_id, slug, worker.request_id)
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
    kind: str = Field(min_length=1, max_length=64)
    delivery: Literal["hooks", "webhook", "poll", "spawn"]
    workdir: str | None = None
    summary: str = Field(default="", max_length=1000)
    tags: list[str] = []
    capabilities: list[dict[str, Any]] = []
    instructions: str | None = Field(default=None, max_length=4000)
    webhook_url: str | None = None
    wakeable: bool | None = None  # omitted keeps the existing value on a takeover
    session_id: str | None = None


class CreateAgentBody(Body):
    slug: str = Field(pattern=SLUG)
    kind: str = Field(default="claude-code", min_length=1, max_length=64)
    workdir: str = Field(min_length=1)
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


class SessionEndBody(Body):
    reason: str = Field(max_length=200)


class AskBody(Body):
    to: str
    task: str = Field(min_length=1)
    capability: str
    context: str | None = None
    thread_id: str | None = Field(default=None, pattern=ULID)
    parent_id: str | None = Field(default=None, pattern=ULID)
    timeout_s: int | None = Field(default=None, ge=1, le=86400)


class ReplyBody(Body):
    result: str
    status: str
    artifacts: list[dict[str, Any]] = []


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


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="coveyd", version=API_VERSION, docs_url=None, redoc_url=None)
    app.state.daemon = Daemon(cfg)

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
        req = router.get_request(app.state.daemon.conn, request_id)
        if who.kind == "agent" and not router.is_participant(req, who.agent_id):
            raise forbidden(
                "You did not send or receive that request.",
                "Read only requests you took part in (`covey_log`).",
            )
        return req

    @app.post("/v1/agents/register")
    async def register_agent(
        body: RegisterBody, who: Annotated[Caller, Depends(require("join"))]
    ) -> dict:
        daemon = app.state.daemon
        card, token = registry.register(
            daemon.conn, daemon.node.node_id, body.model_dump(exclude_none=True)
        )
        await daemon.notify()
        return versioned(card=card, agent_token=token)

    @app.post("/v1/agents", status_code=201)
    async def create_agent(
        body: CreateAgentBody, who: Annotated[Caller, Depends(require("human"))]
    ) -> dict:
        daemon = app.state.daemon
        card = registry.create_agent(
            daemon.conn, daemon.node.node_id, body.model_dump(exclude_none=True)
        )
        await daemon.notify()
        return versioned(card=card)

    @app.post("/v1/agents/{slug}/heartbeat")
    async def heartbeat_agent(
        slug: Annotated[str, Path(pattern=SLUG)],
        who: Annotated[Caller, Depends(require("join", "agent", "human"))],
    ) -> dict:
        daemon = app.state.daemon
        not_worker(who)
        own_slug(who, slug)
        result = registry.heartbeat(daemon.conn, slug)
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
        own_slug(who, slug)
        fields = body.model_dump(exclude_none=True)
        if not fields:
            raise CoveyError(
                400,
                "bad_request",
                "The body has no fields to update.",
                "Send at least one of summary, tags, capabilities, instructions.",
            )
        card = registry.describe(daemon.conn, slug, fields)
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
        own_slug(who, slug)
        agent = registry.get_agent(daemon.conn, slug)
        requests = router.stop_claim(daemon.conn, agent["agent_id"])
        await daemon.notify()
        return versioned(requests=requests)

    @app.put("/v1/agents/{slug}/session")
    async def put_agent_session(
        slug: Annotated[str, Path(pattern=SLUG)],
        body: SessionBody,
        who: Annotated[Caller, Depends(require("agent"))],
    ) -> dict:
        daemon = app.state.daemon
        not_worker(who)
        own_slug(who, slug)
        result = registry.set_session(daemon.conn, slug, body.session_id)
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
        await daemon.notify()
        return versioned(**result)

    @app.post("/v1/requests", status_code=201)
    async def create_request(
        body: AskBody, who: Annotated[Caller, Depends(require("human", "agent"))]
    ) -> dict:
        daemon = app.state.daemon
        req = router.create_request(
            daemon.conn,
            who.agent_id,
            body.to,
            body.capability,
            body.task,
            context=body.context,
            thread_id=body.thread_id,
            # A worker that starts a fresh thread would reset hops; continue its request.
            parent_id=body.parent_id
            or (None if body.thread_id else who.worker_request),
            timeout_s=body.timeout_s,
            max_hops=daemon.cfg.max_hops,
        )
        await daemon.notify()
        daemon.waker.kick()
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

        req = await daemon.wait_for(finished, wait) if wait else None
        return versioned(request=req or router.get_request(daemon.conn, request_id))

    @app.post("/v1/requests/{request_id}/reply")
    async def reply_request(
        request_id: Annotated[str, Path(pattern=ULID)],
        body: ReplyBody,
        who: Annotated[Caller, Depends(require("human", "agent"))],
    ) -> dict:
        daemon = app.state.daemon
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
        return StreamingResponse(
            events.stream(daemon, start),
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


async def _sweeper(daemon: Daemon) -> None:
    """Every sweep_interval_s: apply the liveness timeout, expire due requests, wake waiters."""
    seen: set[str] = set()
    while True:
        await asyncio.sleep(daemon.cfg.sweep_interval_s)
        now = store.now_iso()
        steps = (
            lambda t=now: registry.sweep(daemon.conn, t, daemon.cfg.liveness_timeout_s),
            lambda t=now: router.expire_due(daemon.conn, t),
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
) -> None:
    """Run the app on the unix socket and on 127.0.0.1:PORT until stopped.

    Stops on SIGTERM/SIGINT (main thread only), when `stop` is set, or when cancelled.
    `ready` is set once both servers are listening. Failures to start raise SystemExit
    with a message that says what to do.
    """
    if _socket_answers(cfg.socket_path):
        raise SystemExit(
            f"A coveyd is already running on {cfg.socket_path}. "
            "Run `covey status`, or `covey down` to stop it first."
        )
    tcp_sock = _bind_tcp(cfg)  # fail on a taken port before touching anything else
    app = None
    unix_sock = None
    sweeper: asyncio.Task | None = None
    tasks: list[asyncio.Task] = []
    servers: list[_Server] = []
    try:
        app = create_app(cfg)
        cfg.socket_path.unlink(missing_ok=True)  # stale: nothing answered above
        unix_sock = _bind_unix(cfg.socket_path)
        for sock, transport in ((unix_sock, "unix"), (tcp_sock, "tcp")):
            conf = uvicorn.Config(
                tag_transport(app, transport),
                lifespan="off",
                log_level="warning",
                access_log=False,
            )
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
            app.state.daemon.close()
        for sock in (unix_sock, tcp_sock):
            if sock is not None:
                sock.close()
        cfg.socket_path.unlink(missing_ok=True)


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
    fd = os.open(cfg.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    try:
        asyncio.run(serve(cfg))
    finally:
        cfg.pid_path.unlink(missing_ok=True)
