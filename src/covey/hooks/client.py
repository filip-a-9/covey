"""Hooks' door to the daemon: sync httpx over the unix socket with the agent token."""

from dataclasses import dataclass
from typing import Any

import httpx

from covey.config import Config


class HookError(Exception):
    pass


class HookClient:
    def __init__(self, cfg: Config, token: str, timeout: float = 5.0):
        self._http = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(cfg.socket_path)),
            base_url="http://covey",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The decoded JSON body; HookError on a transport failure or a non-2xx status."""
        try:
            r = self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise HookError(f"coveyd is not reachable: {exc}") from exc
        if r.status_code >= 300:
            raise HookError(f"coveyd answered {r.status_code} to {method} {path}")
        return r.json()

    def close(self) -> None:
        self._http.close()


@dataclass
class HookContext:
    """What a handler needs: this agent's slug and a way to call the daemon (None if the
    daemon is not reachable, e.g. SessionStart before the shim has registered)."""

    slug: str
    client: Any  # HookClient, or any object with the same `call`
