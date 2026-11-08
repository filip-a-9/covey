"""`Agent`: one agent identity on the mesh, over plain blocking httpx.

It registers with the join token, keeps the minted agent token in memory only, heartbeats
from a background thread, and `serve(handler)` polls the inbox and replies. The API has no
long-poll on the inbox (only on `GET /v1/requests/{id}`), so `serve` polls every `poll_s`.
"""

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from covey import config as config_module
from covey.mcp.client import MAJOR, DaemonError

Handler = Callable[[dict[str, Any]], str]


def _log(message: str) -> None:
    print(f"covey sdk: {message}", file=sys.stderr, flush=True)


class Agent:
    def __init__(
        self,
        slug: str,
        *,
        summary: str = "",
        tags: list[str] | None = None,
        capabilities: list[dict[str, str]] | None = None,
        instructions: str | None = None,
        base_url: str | None = None,
        socket_path: str | Path | None = None,
        join_token_path: str | Path | None = None,
        heartbeat_s: float = 20.0,
        poll_s: float = 1.0,
    ):
        cfg = config_module.load()
        self.slug = slug
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.join_token_path = Path(join_token_path or cfg.join_token_path)
        self.token: str | None = None  # in memory only, never written anywhere
        self._card: dict[str, Any] = {
            "slug": slug,
            "kind": "python",
            "delivery": "poll",
            "summary": summary,
            "tags": tags or [],
            "capabilities": capabilities or [],
        }
        if instructions:
            self._card["instructions"] = instructions
        if socket_path:
            transport = httpx.HTTPTransport(uds=str(socket_path))
            self._http = httpx.Client(transport=transport, base_url="http://covey")
        else:
            self._http = httpx.Client(
                base_url=base_url or f"http://127.0.0.1:{cfg.port}"
            )
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- plumbing ---------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def stop(self) -> None:
        """Ask `serve` to return after the request it is handling."""
        self._stop.set()

    def _call(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise DaemonError(
                0,
                "daemon_unreachable",
                f"The covey daemon does not answer at {self._http.base_url} ({exc}).",
                "Run `covey up` in a terminal, then start this agent again.",
            ) from exc
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        version = str(body.get("covey", ""))
        if version.split(".")[0] != MAJOR:
            raise DaemonError(
                r.status_code,
                "version_mismatch",
                f"The daemon speaks covey API {version or 'unknown'}; this SDK speaks {MAJOR}.",
                "Upgrade covey so the daemon and the SDK match, then `covey down && covey up`.",
            )
        if r.status_code >= 400:
            err = body.get("error") or {}
            raise DaemonError(
                r.status_code,
                err.get("code", "error"),
                err.get("message", f"HTTP {r.status_code}"),
                err.get("next_step", "Run `covey doctor`."),
            )
        return body

    def _join_token(self) -> str:
        try:
            return self.join_token_path.read_text().strip()
        except OSError as exc:
            raise DaemonError(
                0,
                "daemon_unreachable",
                f"No join token at {self.join_token_path}; the daemon has never run here.",
                "Run `covey up` in a terminal, then start this agent again.",
            ) from exc

    def register(self) -> None:
        """Register (or take the slug over) and keep the new token in memory."""
        with self._lock:
            body = self._call(
                "POST", "/v1/agents/register", self._join_token(), json=self._card
            )
            self.token = body["agent_token"]

    def call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """Call the daemon as this agent, registering first if needed.

        No re-register on 401: the daemon refuses a takeover while the old holder is live
        (409 slug_live), so a lost token cannot be replaced until liveness expires.
        """
        if self.token is None:
            self.register()
        assert self.token is not None
        return self._call(method, path, self.token, **kw)

    # ---- the API ----------------------------------------------------------------------

    def find(
        self, need: str | None = None, capability: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Matches as the daemon returns them: [{"card": ..., "why": ...}, ...]."""
        params = {"need": need, "capability": capability, "limit": limit}
        return self.call("GET", "/v1/agents", params=params)["matches"]

    def describe(
        self,
        summary: str | None = None,
        tags: list[str] | None = None,
        capabilities: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """Update this agent's card on the daemon; returns the card the daemon now holds.

        `None` leaves a field alone. `capabilities` replaces the whole list ([] clears it).
        The edit also lands in the local card, so a later register() keeps it.
        """
        fields = {
            "summary": summary,
            "tags": tags,
            "capabilities": capabilities,
            "instructions": instructions,
        }
        update = {k: v for k, v in fields.items() if v is not None}
        if not update:
            raise DaemonError(
                400,
                "bad_request",
                "Nothing to change: describe() got no fields.",
                "Pass summary, tags, capabilities and/or instructions.",
            )
        card = self.call("PUT", f"/v1/agents/{self.slug}/card", json=update)["card"]
        with self._lock:
            for key in fields:
                if key in card:
                    self._card[key] = card[key]
        return card

    def ask(self, to: str, task: str, capability: str, **extra: Any) -> dict[str, Any]:
        """Create a request (extra: context, thread_id, parent_id, timeout_s); returns it."""
        body = {"to": to, "task": task, "capability": capability, **extra}
        return self.call("POST", "/v1/requests", json=body)["request"]

    def wait(self, request_id: str, timeout_s: int = 60) -> dict[str, Any]:
        """Block until the request is final or needs input, or `timeout_s` passes."""
        wait = max(0, min(timeout_s, 600))
        path = f"/v1/requests/{request_id}"
        body = self.call("GET", path, params={"wait": wait}, timeout=wait + 10)
        return body["request"]

    def inbox(self) -> list[dict[str, Any]]:
        return self.call("GET", f"/v1/agents/{self.slug}/inbox")["requests"]

    def reply(
        self, request_id: str, result: str, status: str = "done"
    ) -> dict[str, Any]:
        body = {"result": result, "status": status}
        path = f"/v1/requests/{request_id}/reply"
        return self.call("POST", path, json=body)["request"]

    def heartbeat(self) -> None:
        self.call("POST", f"/v1/agents/{self.slug}/heartbeat")

    # ---- the loop ---------------------------------------------------------------------

    def _beat_forever(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            try:
                self.heartbeat()
            except Exception as exc:  # noqa: BLE001 (one bad beat must not end the loop)
                _log(f"heartbeat failed: {exc!r}")

    def serve(self, handler: Handler) -> None:
        """Register, then answer inbox requests with `handler(request) -> str` until stopped.

        A handler that raises answers `failed` with the exception text; the loop goes on.
        """
        self._stop.clear()
        self.register()
        beat = threading.Thread(target=self._beat_forever, daemon=True)
        beat.start()
        handled: set[str] = set()
        try:
            while not self._stop.is_set():
                try:
                    pending = self.inbox()
                except DaemonError as exc:
                    _log(f"inbox failed: {exc.message}")
                    pending = []
                for req in pending:
                    if req["request_id"] in handled or self._stop.is_set():
                        continue
                    handled.add(req["request_id"])
                    self._answer(handler, req)
                self._stop.wait(self.poll_s)
        finally:
            self._stop.set()
            beat.join(2)
            self.close()

    def _answer(self, handler: Handler, req: dict[str, Any]) -> None:
        try:
            result, status = str(handler(req)), "done"
        except Exception as exc:  # noqa: BLE001 (a bad request must not end the agent)
            result, status = f"{type(exc).__name__}: {exc}", "failed"
        try:
            self.reply(req["request_id"], result, status)
        except DaemonError as exc:
            _log(f"reply to {req['request_id']} failed: {exc.message}")


def serve(handler: Handler, slug: str, **agent_args: Any) -> None:
    """`Agent(slug, **agent_args).serve(handler)`: join the mesh and answer until Ctrl-C."""
    try:
        Agent(slug, **agent_args).serve(handler)
    except KeyboardInterrupt:
        pass
