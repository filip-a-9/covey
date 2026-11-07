"""The one place Covey reads its configuration from.

Environment variables:
    COVEY_HOME  directory for the database, tokens, socket, pidfile and log (default ~/.covey)
    COVEY_PORT  TCP port on 127.0.0.1 (default 7777)
    COVEY_CLAUDE        the `claude` executable headless workers run (default `claude` on PATH)
    COVEY_WORKER_MODEL  model for headless workers (default `sonnet`: the default model can
                        spend more than a worker's budget in its first turns)
    COVEY_NOTIFY        `auto` (desktop notification where one is available, always logged) or
                        `log` (log only) (default `auto`)
    COVEY_WAKE_SOCKET_DIR  the one directory a session's messaging socket may live in
                        (default $XDG_RUNTIME_DIR/cc-socks, else /run/user/<uid>/cc-socks)
    COVEY_MAX_HOPS      delegation depth limit, 0 to 4 (default 4)
    COVEY_IDLE_WAKE     `off` stops the daemon posting to idle sessions' messaging sockets
                        (default on)
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MAX_HOPS_LIMIT = 4  # the requests.hops CHECK in store.py


class ConfigError(ValueError):
    pass


def default_wake_socket_dir(env: Mapping[str, str]) -> Path:
    """Where Claude Code puts its messaging sockets: `cc-socks` in the user's runtime dir."""
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "cc-socks"


@dataclass(frozen=True)
class Config:
    home: Path
    port: int
    liveness_timeout_s: float = 60.0
    sweep_interval_s: float = 5.0
    max_hops: int = 4
    max_body_bytes: int = 1024 * 1024  # larger HTTP bodies get 413, before auth
    max_open_gated: int = 10  # awaiting_grant requests one requester may have open
    claude_bin: str = "claude"
    worker_model: str = "sonnet"
    # Direct Config(...) callers (tests, embedders) never get a desktop toast; load() says `auto`.
    notify: str = "log"
    max_workers: int = 2  # headless workers running at once, across all agents
    worker_timeout_s: float = (
        600.0  # wall-clock kill, or the request deadline if sooner
    )

    # A wake socket outside this directory is ignored: the daemon writes to it, so an agent
    # must not be able to name any path it likes.
    wake_socket_dir: Path = field(
        default_factory=lambda: default_wake_socket_dir(os.environ)
    )
    idle_wake: bool = True  # post queued requests to a live session's wake socket
    idle_wake_fallback_s: float = (
        600.0  # a wake or Stop claim unread this long is re-offered
    )

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

    @property
    def workers_path(self) -> Path:
        return self.home / "workers"


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
    notify = env.get("COVEY_NOTIFY") or "auto"
    if notify not in ("auto", "log"):
        raise ConfigError(
            f"COVEY_NOTIFY={notify!r} is not a mode. Set it to `auto` or `log`."
        )
    idle_wake = env.get("COVEY_IDLE_WAKE") or "on"
    if idle_wake not in ("on", "off"):
        raise ConfigError(
            f"COVEY_IDLE_WAKE={idle_wake!r} is not a mode. Set it to `on` or `off`."
        )
    raw_hops = env.get("COVEY_MAX_HOPS") or "4"
    try:
        max_hops = int(raw_hops)
    except ValueError:
        max_hops = -1
    if not 0 <= max_hops <= MAX_HOPS_LIMIT:
        raise ConfigError(
            f"COVEY_MAX_HOPS={raw_hops!r} is out of range. Set it to a number between"
            f" 0 and {MAX_HOPS_LIMIT} (the database refuses more)."
        )
    return Config(
        home=home,
        max_hops=max_hops,
        idle_wake=idle_wake == "on",
        wake_socket_dir=Path(
            env.get("COVEY_WAKE_SOCKET_DIR") or default_wake_socket_dir(env)
        ),
        notify=notify,
        port=port,
        claude_bin=env.get("COVEY_CLAUDE") or "claude",
        worker_model=env.get("COVEY_WORKER_MODEL") or "sonnet",
    )
