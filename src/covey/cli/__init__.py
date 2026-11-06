"""`covey`: the human's command line. Talks to coveyd over the unix socket with the human token."""

import json
import os
import signal
import time
from typing import Annotated, Any

import httpx
import typer

from covey import config as config_module
from covey.cli.client import human_client
from covey.cli.hook import hook_app
from covey.config import Config, ConfigError
from covey.launch import LaunchError, ensure_daemon, pid_alive, read_pid

app = typer.Typer(
    help="Covey: a local agent mesh.", no_args_is_help=True, add_completion=False
)

app.add_typer(hook_app, name="hook")

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
    try:
        ensure_daemon(cfg)
    except LaunchError as exc:
        raise fail(as_json, str(exc), log=str(cfg.log_path)) from exc
    info = running_info(cfg)
    if info is None:
        raise fail(
            as_json,
            f"coveyd answers but refuses the human token in {cfg.token_path}. Either that "
            "file belongs to another daemon (check COVEY_HOME), or this command runs inside "
            "an agent session: run `covey up` from your own terminal. "
            f"Log: {cfg.log_path}",
            log=str(cfg.log_path),
        )
    out(as_json, info, f"coveyd is running (pid {info['pid']}, port {cfg.port}).")


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


@app.command()
def mcp() -> None:
    """Run the MCP stdio server for this session (Claude Code starts this, not you)."""
    from covey.mcp.server import main  # lazy: the rest of the CLI skips the MCP SDK

    main()
