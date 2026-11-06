"""The shim's door to the daemon: async httpx over the unix socket, one bearer token per call.

Every daemon failure becomes a `DaemonError` carrying the contract's `code`, `message` and
`next_step`, so the tools can hand the agent something it can act on.
"""

from typing import Any

import httpx

from covey.config import Config

MAJOR = "1"


class DaemonError(Exception):
    def __init__(self, status: int, code: str, message: str, next_step: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.next_step = next_step


class AgentClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(cfg.socket_path)),
            base_url="http://covey",
            timeout=10.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
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
            r = await self._http.request(
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
                f"The covey daemon does not answer on {self.cfg.socket_path} ({exc}).",
                "Ask the user to run `covey up` in their terminal, then call this tool again.",
            ) from exc
        try:
            body = r.json()
        except ValueError as exc:
            raise DaemonError(
                r.status_code,
                "bad_response",
                f"The daemon sent a non-JSON answer (HTTP {r.status_code}).",
                "Ask the user to run `covey doctor`.",
            ) from exc
        if not isinstance(body, dict):
            body = {}
        version = str(body.get("covey", ""))
        if version.split(".")[0] != MAJOR:
            raise DaemonError(
                r.status_code,
                "version_mismatch",
                f"The daemon speaks covey API {version or 'unknown'}; this shim speaks {MAJOR}.",
                "Ask the user to upgrade covey so the daemon and the shim match, "
                "then restart the daemon (`covey down && covey up`).",
            )
        if r.status_code >= 400:
            err = body.get("error") or {}
            raise DaemonError(
                r.status_code,
                err.get("code", "error"),
                err.get("message", f"HTTP {r.status_code}"),
                err.get("next_step", "Ask the user to run `covey doctor`."),
            )
        return body
