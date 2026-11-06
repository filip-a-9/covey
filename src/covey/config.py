"""The one place Covey reads its configuration from.

Environment variables:
    COVEY_HOME  directory for the database, tokens, socket, pidfile and log (default ~/.covey)
    COVEY_PORT  TCP port on 127.0.0.1 (default 7777)
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    home: Path
    port: int
    liveness_timeout_s: float = 60.0
    sweep_interval_s: float = 5.0
    max_hops: int = 4

    @property
    def db_path(self) -> Path:
        return self.home / "covey.db"

    @property
    def socket_path(self) -> Path:
        return self.home / "covey.sock"

    @property
    def token_path(self) -> Path:
        return self.home / "token"

    @property
    def join_token_path(self) -> Path:
        return self.home / "join-token"

    @property
    def node_path(self) -> Path:
        return self.home / "node.json"

    @property
    def pid_path(self) -> Path:
        return self.home / "coveyd.pid"

    @property
    def log_path(self) -> Path:
        return self.home / "coveyd.log"


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = Path(env.get("COVEY_HOME") or Path.home() / ".covey").expanduser()
    raw_port = env.get("COVEY_PORT") or "7777"
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"COVEY_PORT={raw_port!r} is not a TCP port. Set it to a number between 1 and 65535."
        )
    return Config(home=home, port=port)
