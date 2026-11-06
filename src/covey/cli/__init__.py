"""`covey`: the human's command line. Talks to coveyd over the unix socket with the human token."""

import json
import os
import signal
import subprocess
import sys
import time
from typing import Annotated, Any

import httpx
import typer

from covey import config as config_module
from covey.cli.client import human_client
from covey.config import Config, ConfigError

app = typer.Typer(
    help="Covey: a local agent mesh.", no_args_is_help=True, add_completion=False
)

JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def load_cfg() -> Config:
    try:
        return config_module.load()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def out(as_json: bool, payload: dict[str, Any], text: str) -> None:
    typer.echo(json.dumps(payload) if as_json else text)


def fail(as_json: bool, message: str, **extra: Any) -> typer.Exit:
    """Print an error (with its next step in the message) and return Exit(1) to raise."""
    if as_json:
        typer.echo(json.dumps({"error": message, **extra}))
    else:
        typer.echo(message, err=True)
    return typer.Exit(1)


def read_pid(cfg: Config) -> int | None:
    try:
        return int(cfg.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our own exited child: reap it
            return False
    except ChildProcessError:
        pass  # not our child, the normal case across two `covey` runs
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def whoami(cfg: Config) -> dict[str, Any] | None:
    """GET /v1/whoami as the human; None if the daemon does not answer."""
    if not cfg.socket_path.exists() or not cfg.token_path.exists():
        return None
    try:
        with human_client(cfg, timeout=2.0) as c:
            r = c.get("/v1/whoami")
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None


def last_log_lines(cfg: Config, n: int = 15) -> str:
    try:
        return "\n".join(cfg.log_path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log yet)"


def running_info(cfg: Config) -> dict[str, Any] | None:
    who = whoami(cfg)
    if who is None:
        return None
    return {
        "running": True,
        "pid": read_pid(cfg),
        "node_id": who["node_id"],
        "socket": str(cfg.socket_path),
        "port": cfg.port,
    }


@app.command()
def up(
    foreground: Annotated[
        bool, typer.Option("--foreground", help="Run in this terminal.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Start the daemon."""
    cfg = load_cfg()
    info = running_info(cfg)
    if info:
        out(as_json, info, f"coveyd is already running (pid {info['pid']}).")
        return
    if foreground:
        from covey.daemon.app import main

        main()
        return
    cfg.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(cfg.log_path, "ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "covey.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise fail(
                as_json,
                f"coveyd exited with code {child.returncode}. Last log lines:\n"
                f"{last_log_lines(cfg)}\nFix the problem above, then run `covey up` again. "
                f"Full log: {cfg.log_path}",
                log=str(cfg.log_path),
            )
        info = running_info(cfg)
        if info:
            out(
                as_json,
                info,
                f"coveyd is running (pid {info['pid']}, port {cfg.port}).",
            )
            return
        time.sleep(0.1)
    raise fail(
        as_json,
        f"coveyd did not answer within 10 s. Check the log at {cfg.log_path}, "
        "then run `covey down` and `covey up` again.",
        log=str(cfg.log_path),
    )


@app.command()
def down(as_json: JsonOpt = False) -> None:
    """Stop the daemon."""
    cfg = load_cfg()
    pid = read_pid(cfg)
    if pid is None or not pid_alive(pid):
        stale = pid is not None
        cfg.pid_path.unlink(missing_ok=True)
        out(
            as_json,
            {"running": False, "stale_pidfile_removed": stale},
            "coveyd is not running." + (" Removed a stale pidfile." if stale else ""),
        )
        return
    if running_info(cfg) is None:
        # The pid is alive but no coveyd answers on the socket: after a crash or reboot the
        # OS may have given that pid to an unrelated process, so never signal it blindly.
        raise fail(
            as_json,
            f"The pidfile names pid {pid}, but no coveyd answers on {cfg.socket_path}. "
            f"If `ps -p {pid}` shows coveyd, stop it with `kill {pid}`; otherwise delete "
            f"{cfg.pid_path}.",
            pid=pid,
        )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            cfg.pid_path.unlink(missing_ok=True)
            out(as_json, {"running": False, "stopped_pid": pid}, "coveyd stopped.")
            return
        time.sleep(0.1)
    raise fail(
        as_json,
        f"coveyd (pid {pid}) did not stop within 10 s. "
        f"Check {cfg.log_path}, then run `kill -9 {pid}`.",
        pid=pid,
    )


@app.command()
def status(as_json: JsonOpt = False) -> None:
    """Show whether the daemon is running."""
    cfg = load_cfg()
    info = running_info(cfg)
    if info is None:
        message = "coveyd is not running. Run `covey up`."
        if as_json:
            typer.echo(json.dumps({"running": False, "next_step": "Run `covey up`."}))
        else:
            typer.echo(message)
        raise typer.Exit(1)
    out(
        as_json,
        info,
        f"coveyd is running\n  pid:     {info['pid']}\n  node_id: {info['node_id']}\n"
        f"  socket:  {info['socket']}\n  port:    {info['port']}",
    )


def slug(agent_id: str) -> str:
    return agent_id.partition("@")[0]


def format_log(payload: dict[str, Any]) -> str:
    """A threaded transcript: requests grouped by thread, oldest thread first, result indented."""
    requests = payload.get("requests", [])
    if not requests:
        return "No requests."
    threads: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(requests, key=lambda r: r["created_at"]):
        threads.setdefault(r["thread_id"], []).append(r)
    blocks = []
    for thread_id, rows in threads.items():
        lines = [f"thread {thread_id}"]
        for r in rows:
            clock = r["created_at"][11:19]
            lines.append(
                f"{clock}  {slug(r['from_agent'])} -> {slug(r['to_agent'])}  "
                f"{r['capability']}  {r['status']}  {r['task']}"
            )
            if r.get("result"):
                lines.append(f"    {r['result']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@app.command()
def log(
    thread: Annotated[str | None, typer.Option(help="Only this thread id.")] = None,
    agent: Annotated[str | None, typer.Option(help="Only this agent.")] = None,
    since: Annotated[str | None, typer.Option(help="ISO-8601 time.")] = None,
    limit: Annotated[int, typer.Option(help="Most requests to show.")] = 50,
    as_json: JsonOpt = False,
) -> None:
    """Show the request transcript."""
    cfg = load_cfg()
    params = {"thread": thread, "agent": agent, "since": since, "limit": limit}
    params = {k: v for k, v in params.items() if v is not None}
    if whoami(cfg) is None:
        raise fail(as_json, "coveyd is not running. Run `covey up`.")
    with human_client(cfg) as c:
        r = c.get("/v1/log", params=params)
    if r.status_code != 200:
        raise fail(
            as_json,
            f"The daemon refused the log query (HTTP {r.status_code}): {r.text} "
            "Check the options, or run `covey status`.",
        )
    payload = r.json()
    out(as_json, payload, format_log(payload))
