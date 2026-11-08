"""`covey`: the human's command line. Talks to coveyd over the unix socket with the human token."""

import json
import os
import signal
import sys
import time
from importlib import metadata
from typing import Annotated, Any

import httpx
import typer

from covey import config as config_module
from covey.cli.client import human_client
from covey.cli.hook import hook_app
from covey.cli.init import init as init_command
from covey.config import Config, ConfigError
from covey.launch import LaunchError, ensure_daemon, pid_alive, read_pid
from covey.text import safe

app = typer.Typer(
    help="Covey: a local agent mesh.", no_args_is_help=True, add_completion=False
)


def show_version(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(f"covey-mesh {metadata.version('covey-mesh')}")
    except metadata.PackageNotFoundError:
        typer.echo("covey-mesh unknown (not installed; run uv sync)")
    raise typer.Exit()


@app.callback()
def refuse_windows(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=show_version,
            is_eager=True,
            help="Print the installed version and exit.",
        ),
    ] = False,
) -> None:
    if sys.platform == "win32":
        typer.echo("Covey needs Linux or macOS for now; on Windows use WSL.", err=True)
        raise typer.Exit(2)


app.add_typer(hook_app, name="hook")
app.command("init")(init_command)

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


def probe(cfg: Config) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """GET /v1/whoami as the human: (body, None) on success, (None, error) if the daemon
    refuses (such as `human_only` from inside an agent session), (None, None) if it does
    not answer."""
    if not cfg.socket_path.exists() or not cfg.token_path.exists():
        return None, None
    try:
        with human_client(cfg, timeout=2.0) as c:
            r = c.get("/v1/whoami")
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return None, None
    if r.status_code == 200:
        return body, None
    return None, (body.get("error") if isinstance(body, dict) else None)


def whoami(cfg: Config, as_json: bool = False) -> dict[str, Any] | None:
    """The daemon's whoami, or None if it does not answer. A refusal is printed with the
    daemon's reason, and the command exits."""
    body, err = probe(cfg)
    if err:
        message = f"{err.get('message', '')} {err.get('next_step', '')}".strip()
        raise fail(as_json, message, code=err.get("code"))
    return body


def running_info(cfg: Config, as_json: bool = False) -> dict[str, Any] | None:
    who = whoami(cfg, as_json)
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
    info = running_info(cfg, as_json)
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
    info = running_info(cfg, as_json)
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
    if running_info(cfg, as_json) is None:
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
    info = running_info(cfg, as_json)
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


# Already shown by the request line and its result.
SHOWN_BY_REQUEST = ("request.created", "request.replied")
DETAIL_KEYS = ("scope", "code", "reason")


def event_detail(event: dict[str, Any]) -> str:
    detail = event.get("detail") or {}
    return "  ".join(f"{k}={safe(detail[k])}" for k in DETAIL_KEYS if k in detail)


def format_event(event: dict[str, Any]) -> str:
    """`HH:MM:SS  event  actor  k=v`: one audit row, the way `covey log` shows it."""
    parts = [event["ts"][11:19], safe(event["event"]), safe(slug(event["actor"]))]
    return "  ".join(p for p in [*parts, event_detail(event)] if p)


def format_follow(event: dict[str, Any], request: dict[str, Any]) -> list[str]:
    """The lines `covey log --follow` prints for one new audit row about `request`."""
    head = (
        f"{event['ts'][11:19]}  {request['thread_id'][-6:]}  "
        f"{safe(slug(request['from_agent']))} -> {safe(slug(request['to_agent']))}"
        f"  {safe(event['event'])}"
    )
    if event["event"] == "request.created":
        return [f"{head}  {safe(request['task'])}"]
    if event["event"] == "request.replied":
        lines = [f"{head}  {request['status']}"]
        result = request.get("result")
        return lines + ([f"    {safe(result)}"] if result else [])
    detail = event_detail(event)
    return [f"{head}  {detail}" if detail else head]


def format_log(payload: dict[str, Any]) -> str:
    """A threaded transcript: requests grouped by thread, oldest thread first; beneath each
    request its audit events, then its result."""
    requests = payload.get("requests", [])
    if not requests:
        return "No requests."
    by_request: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(payload.get("events", []), key=lambda e: e["id"]):
        if e["event"] not in SHOWN_BY_REQUEST:
            by_request.setdefault(e["subject"], []).append(e)
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
                f"{r['capability']}  {r['status']}  {safe(r['task'])}"
            )
            lines += [
                f"    {format_event(e)}" for e in by_request.get(r["request_id"], [])
            ]
            if r.get("result"):
                lines.append(f"    {safe(r['result'])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# Audit rows about a request; the rest (agents, grants) stay out of the transcript.
REQUEST_EVENTS = ("request.", "approval.", "worker.")
# The daemon sends a keepalive every 15 s; a minute of silence means the stream is gone.
STREAM_TIMEOUT = httpx.Timeout(5.0, read=60.0)


def emit(line: str) -> None:
    typer.echo(line)
    sys.stdout.flush()  # `covey log --follow | grep` should see each line as it happens


def follow_log(
    cfg: Config, params: dict[str, Any], as_json: bool, payload: dict
) -> None:
    """After the transcript: print each new audit row about a request, until Ctrl-C.

    Every frame on /v1/events (and each (re)connect) re-reads /v1/log with the user's
    filters, so the daemon decides what is in scope; rows already printed are skipped by id.
    """
    events = payload.get("events", [])
    last = max(events, key=lambda e: e["id"], default=None)
    state = {"id": last["id"] if last else 0, "ts": last["ts"] if last else None}

    def catch_up(c: httpx.Client) -> None:
        query = {**params, "limit": 500}
        if state["ts"]:
            query["since"] = state["ts"]
        found = c.get("/v1/log", params=query).json()
        for e in sorted(found.get("events", []), key=lambda e: e["id"]):
            if e["id"] <= state["id"]:
                continue
            state["id"], state["ts"] = e["id"], e["ts"]
            if not e["event"].startswith(REQUEST_EVENTS):
                continue
            if as_json:
                emit(json.dumps(e))
                continue
            req = c.get(f"/v1/requests/{e['subject']}").json()["request"]
            for line in format_follow(e, req):
                emit(line)

    try:
        with human_client(cfg, timeout=STREAM_TIMEOUT) as c:
            while True:
                try:
                    with c.stream("GET", "/v1/events") as r:
                        r.raise_for_status()
                        for line in r.iter_lines():
                            # The opening comment marks a (re)connect: catch up on the gap.
                            if line.startswith("data:") or line == ": covey events":
                                catch_up(c)
                except httpx.HTTPError as exc:
                    typer.echo(f"Event stream lost ({exc}); reconnecting.", err=True)
                time.sleep(2)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


@app.command()
def log(
    thread: Annotated[str | None, typer.Option(help="Only this thread id.")] = None,
    agent: Annotated[str | None, typer.Option(help="Only this agent.")] = None,
    since: Annotated[str | None, typer.Option(help="ISO-8601 time.")] = None,
    limit: Annotated[int, typer.Option(help="Most requests to show.")] = 50,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new events.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show the request transcript, with each request's audit events."""
    cfg = load_cfg()
    params = {"thread": thread, "agent": agent, "since": since, "limit": limit}
    params = {k: v for k, v in params.items() if v is not None}
    if whoami(cfg, as_json) is None:
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
    if follow:
        sys.stdout.flush()
        follow_log(cfg, params, as_json, payload)


@app.command()
def mcp(
    worker: Annotated[
        bool,
        typer.Option("--worker", help="Serve a headless worker that coveyd spawned."),
    ] = False,
) -> None:
    """Run the MCP stdio server for this session (Claude Code starts this, not you)."""
    from covey.mcp.server import main  # lazy: the rest of the CLI skips the MCP SDK

    main(worker=worker)


@app.command()
def top() -> None:
    """Live terminal view of agents, activity and approvals."""
    try:
        from covey.tui.app import run  # lazy: Textual is an optional extra
    except ImportError as exc:
        typer.echo(
            "`covey top` needs the tui extra. Reinstall with "
            "`uv tool install --force 'covey-mesh[tui]'` "
            "(or `uv sync --extra tui` in a checkout).",
            err=True,
        )
        raise typer.Exit(1) from exc
    cfg = load_cfg()
    if whoami(cfg) is None:
        typer.echo("coveyd is not running. Run `covey up`, then `covey top`.", err=True)
        raise typer.Exit(1)
    run(cfg)


from covey.cli import commands  # noqa: F401  (registers the commands on `app`)
