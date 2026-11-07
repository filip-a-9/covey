"""Who this shim is: its slug, the `claude` process it serves, and the token file hooks read.

Rules (docs/SPEC.md, "How hooks authenticate"):
- Slug: `COVEY_AGENT` if set, else the working directory's name squeezed into the slug pattern.
- The `claude` process is the nearest ancestor whose argv0 is `claude`; a launcher such as
  `uv run` may sit in between. Its pid survives `/clear`, unlike the session id.
- The token file is `<COVEY_HOME>/sessions/claude-<pid>.json`, mode 0600, and carries the
  process start time so a recycled pid does not match.
"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from covey.config import Config
from covey.procs import ClaudeProcess, _parent_and_argv0, _ps, _start_time, find_claude

__all__ = ["ClaudeProcess", "_parent_and_argv0", "_ps", "_start_time", "find_claude"]


class IdentityError(Exception):
    def __init__(self, message: str, next_step: str):
        super().__init__(message)
        self.message = message
        self.next_step = next_step


def derive_slug(env: Mapping[str, str], cwd: Path) -> str:
    explicit = env.get("COVEY_AGENT")
    if explicit:
        return explicit
    slug = re.sub(r"[^a-z0-9_-]+", "-", cwd.name.lower()).lstrip("_-")[:63]
    if not slug:
        raise IdentityError(
            f"Cannot make an agent name from the directory name {cwd.name!r}.",
            "Set COVEY_AGENT=<slug> in this session's MCP server env "
            "(lowercase letters, digits, - and _).",
        )
    return slug


def session_file(cfg: Config, claude: ClaudeProcess) -> Path:
    return cfg.home / "sessions" / f"claude-{claude['pid']}.json"


def write_session_file(
    cfg: Config, claude: ClaudeProcess, slug: str, agent_token: str
) -> Path:
    sessions = cfg.home / "sessions"
    sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
    sessions.chmod(0o700)  # mkdir's mode only applies when it creates the directory
    path = session_file(cfg, claude)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # O_CREAT's mode only applies to a new file
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "slug": slug,
                "agent_token": agent_token,
                "claude_pid": claude["pid"],
                "claude_start_time": claude["start_time"],
            },
            f,
        )
    os.replace(tmp, path)
    return path


class SessionInfo(TypedDict):
    slug: str
    agent_token: str


def read_session(cfg: Config, env: Mapping[str, str]) -> SessionInfo | None:
    """What a hook may act as: the shim's token file for the `claude` process in $CLAUDE_PID.

    None when there is no file (no shim yet), it is unreadable, or its start time does not
    match the live process (a recycled pid).
    """
    try:
        pid = int(env["CLAUDE_PID"])
        data = json.loads(session_file(cfg, {"pid": pid, "start_time": ""}).read_text())
        if data["claude_pid"] != pid or data["claude_start_time"] != _start_time(pid):
            return None
        return {"slug": data["slug"], "agent_token": data["agent_token"]}
    except (KeyError, ValueError, OSError):
        return None


def wake_file(cfg: Config, claude_pid: int) -> Path:
    return cfg.home / "sessions" / f"claude-{claude_pid}.wake.json"


def write_wake_file(
    cfg: Config, claude_pid: int, start_time: str | None, socket_path: str
) -> Path:
    """Where the SessionStart hook leaves Claude Code's messaging socket path for the shim.
    No token: the socket does not check one."""
    sessions = cfg.home / "sessions"
    sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
    sessions.chmod(0o700)
    path = wake_file(cfg, claude_pid)
    tmp = path.with_suffix(
        ".tmp"
    )  # claude-<pid>.wake.tmp, never the session file's tmp
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "socket": socket_path,
                "claude_pid": claude_pid,
                "claude_start_time": start_time,
            },
            f,
        )
    os.replace(tmp, path)
    return path


def read_wake_file(cfg: Config, claude: ClaudeProcess) -> str | None:
    """The socket path for this `claude` process, or None on any problem or a start-time
    mismatch (a recycled pid)."""
    try:
        data = json.loads(wake_file(cfg, claude["pid"]).read_text())
        if data["claude_start_time"] != claude["start_time"]:
            return None
        socket = data["socket"]
        # Over the API's 200-character limit, register would fail with 400: skip idle wake.
        ok = isinstance(socket, str) and 0 < len(socket) <= 200
        return socket if ok else None
    except (KeyError, ValueError, OSError, TypeError):
        return None


def forget_wake_file(cfg: Config, claude: ClaudeProcess) -> None:
    """Remove the wake file if it belongs to this `claude` process."""
    try:
        path = wake_file(cfg, claude["pid"])
        data = json.loads(path.read_text())
        if (
            data["claude_pid"] == claude["pid"]
            and data["claude_start_time"] == claude["start_time"]
        ):
            path.unlink()
    except (KeyError, ValueError, OSError, TypeError):
        pass
