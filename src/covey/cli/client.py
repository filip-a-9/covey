"""The CLI's door to the daemon: httpx over the unix socket with the human token."""

import httpx

from covey.config import Config


def human_client(cfg: Config, timeout: float | httpx.Timeout = 5.0) -> httpx.Client:
    token = cfg.token_path.read_text().strip()
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(cfg.socket_path)),
        base_url="http://covey",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
