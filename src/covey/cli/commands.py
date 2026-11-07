"""The human's everyday commands: agents, requests, approvals, grants and `doctor`.

Registered on the app in `covey.cli`. Each one calls the daemon over the unix socket with
the human token and prints a table, or the daemon's JSON with `--json`.
"""

import json
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from covey.cli import JsonOpt, app, fail, load_cfg, out, probe, slug, whoami
from covey.cli.client import human_client
from covey.cli.init import HOOKS, hook_runs, is_covey_hook
from covey.launch import daemon_answers
from covey.text import safe

FINAL = ("done", "failed", "refused", "expired")


def call(
    as_json: bool, method: str, path: str, timeout: float = 5.0, **kw: Any
) -> dict[str, Any]:
    """One daemon call as the human; on any failure raise an Exit with the next step."""
    cfg = load_cfg()
    if whoami(cfg, as_json) is None:
        raise fail(as_json, "coveyd is not running. Run `covey up`.")
    try:
        with human_client(cfg, timeout=timeout) as c:
            r = c.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise fail(
            as_json,
            f"The call to coveyd failed ({exc}). Run `covey status`, then retry.",
        ) from exc
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        err = body.get("error", {})
        message = f"{err.get('message', r.text)} {err.get('next_step', '')}".strip()
        raise fail(as_json, message if as_json else safe(message), code=err.get("code"))
    return body


def table(rows: list[list[str]], empty: str) -> str:
    if not rows:
        return empty
    rows = [[safe(cell) for cell in r] for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
        for r in rows
    )


def request_line(r: dict[str, Any]) -> list[str]:
    return [
        r["request_id"],
        f"{slug(r['from_agent'])} -> {slug(r['to_agent'])}",
        r["capability"],
        r["status"],
        r["task"],
    ]


def format_requests(requests: list[dict[str, Any]], empty: str) -> str:
    return table([request_line(r) for r in requests], empty)


def format_cards(cards: list[dict[str, Any]], extra: list[str] | None = None) -> str:
    rows = []
    for i, c in enumerate(cards):
        caps = ",".join(f"{x['name']}:{x['level']}" for x in c.get("capabilities", []))
        rows.append(
            [
                c["slug"],
                c["kind"],
                c["liveness"],
                c.get("workdir") or "-",
                caps or "-",
                c["last_seen"][:19],
            ]
            + ([extra[i]] if extra else [])
        )
    return table(rows, "No agents.")


@app.command()
def ps(as_json: JsonOpt = False) -> None:
    """List agents that are not gone."""
    payload = call(as_json, "GET", "/v1/agents", params={"limit": 50})
    agents = [m["card"] for m in payload["matches"]]
    out(as_json, {"covey": "1", "agents": agents}, format_cards(agents))


@app.command()
def find(
    need: Annotated[str, typer.Argument(help="What you need done.")],
    capability: Annotated[
        str | None, typer.Option(help="Require this capability.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Most matches to show (1-50).")] = 5,
    as_json: JsonOpt = False,
) -> None:
    """Rank agents for a need, with the reason each matched."""
    params: dict[str, Any] = {"need": need, "limit": limit}
    if capability:
        params["capability"] = capability
    payload = call(as_json, "GET", "/v1/agents", params=params)
    matches = payload["matches"]
    text = format_cards([m["card"] for m in matches], [m["why"] for m in matches])
    out(as_json, payload, text if matches else "No agent matches. Run `covey ps`.")


agent_app = typer.Typer(
    help="Manage agents that have no session.", no_args_is_help=True
)
app.add_typer(agent_app, name="agent")


def csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


LEVELS = ("open", "gated", "forbidden")


def parse_capabilities(as_json: bool, value: str) -> list[dict[str, str]]:
    """`explain:open,run_tests:gated` -> card capabilities; Exit with the format on error."""
    caps = []
    for item in csv(value):
        name, _, level = item.partition(":")
        if level.strip() not in LEVELS:
            raise fail(
                as_json,
                f"Capability {item!r} is not name:level. Use e.g. "
                f"`--capabilities explain:open,run_tests:gated` (levels: {', '.join(LEVELS)}).",
            )
        caps.append({"name": name.strip(), "level": level.strip()})
    return caps


@agent_app.command("add")
def agent_add(
    agent: Annotated[str, typer.Argument(help="Agent slug.")],
    workdir: Annotated[
        Path, typer.Option(help="The agent's repo; a worker runs here.")
    ] = Path("."),
    wakeable: Annotated[
        bool, typer.Option("--wakeable", help="Spawn a headless worker when asked.")
    ] = False,
    kind: Annotated[str, typer.Option(help="Agent kind.")] = "claude-code",
    summary: Annotated[str | None, typer.Option(help="What the agent does.")] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags.")] = None,
    capabilities: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated name:level, e.g. explain:open,run_tests:gated."
        ),
    ] = None,
    allowed_tools: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated Claude Code tools and rules for the worker, e.g. "
            "`Read,Edit(docs/**),Bash(git status:*)`. Bash only if you list it."
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            help="Turn cap for one worker run. "
            "Claude Code stops after the turn that passes it."
        ),
    ] = None,
    max_budget_usd: Annotated[
        float | None,
        typer.Option(
            help="Spend cap in USD for one worker run. "
            "Checked after each turn, so one turn can go over it."
        ),
    ] = None,
    daily_spawns: Annotated[
        int | None, typer.Option(help="Most workers in any rolling 24 hours.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Pre-register an agent with no session, or update one that is not live."""
    path = workdir.expanduser().resolve()
    if not path.is_dir():
        raise fail(
            as_json, f"{path} is not a directory. Pass the agent's repo as --workdir."
        )
    home = load_cfg().home.resolve()
    if path == Path.home().resolve() or path == home or path in home.parents:
        raise fail(
            as_json,
            f"{path} is your home or contains {home}, so a worker could read the daemon's"
            " tokens. Pass the agent's repo as --workdir.",
        )
    body: dict[str, Any] = {
        "slug": agent,
        "kind": kind,
        "workdir": str(path),
        "wakeable": wakeable,
    }
    if summary is not None:
        body["summary"] = summary
    if tags is not None:
        body["tags"] = csv(tags)
    if capabilities is not None:
        body["capabilities"] = parse_capabilities(as_json, capabilities)
    worker = {
        "allowed_tools": csv(allowed_tools) if allowed_tools is not None else None,
        "max_turns": max_turns,
        "max_budget_usd": max_budget_usd,
        "daily_spawns": daily_spawns,
    }
    worker = {k: v for k, v in worker.items() if v is not None}
    if worker:
        body["worker"] = worker
    payload = call(as_json, "POST", "/v1/agents", json=body)
    card = payload["card"]
    out(as_json, payload, f"Added {agent} ({card['liveness']}) in {path}.")


@app.command()
def inbox(
    agent: Annotated[str, typer.Argument(help="Agent slug.")],
    as_json: JsonOpt = False,
) -> None:
    """Show what is waiting for an agent (read-only: nothing is marked delivered)."""
    payload = call(as_json, "GET", f"/v1/agents/{agent}/inbox", params={"peek": "true"})
    out(as_json, payload, format_requests(payload["requests"], "Nothing waiting."))


CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")  # card.json pattern


def parse_capability(as_json: bool, item: str) -> dict[str, str]:
    """`name=level[:note]` -> one card capability; Exit with the format on error."""
    name, eq, rest = item.partition("=")
    level, _, note = rest.partition(":")
    name, level, note = name.strip(), level.strip(), note.strip()
    if not eq or level not in LEVELS or not CAPABILITY_NAME.fullmatch(name):
        raise fail(
            as_json,
            f"Capability {item!r} is not name=level[:note]. Use e.g. "
            f"`--capability run_tests=gated:slow` (levels: {', '.join(LEVELS)}; "
            "name: lowercase letters, digits, _ . -).",
        )
    cap = {"name": name, "level": level}
    if note:
        cap["note"] = note
    return cap


@app.command()
def describe(
    agent: Annotated[str, typer.Argument(help="Agent slug.")],
    summary: Annotated[str | None, typer.Option(help="New summary.")] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags.")] = None,
    capability: Annotated[
        list[str] | None,
        typer.Option(
            help="name=level[:note], repeatable, e.g. run_tests=gated. Replaces the "
            "agent's whole capability list, as covey_describe does."
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Edit an agent's card by hand."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if tags is not None:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if capability:
        body["capabilities"] = [parse_capability(as_json, c) for c in capability]
    if not body:
        raise fail(
            as_json,
            "Nothing to change. Pass --summary, --tags and/or --capability.",
        )
    payload = call(as_json, "PUT", f"/v1/agents/{agent}/card", json=body)
    out(as_json, payload, f"Updated the card for {agent}.")


@app.command()
def ask(
    agent: Annotated[str, typer.Argument(help="Target slug.")],
    task: Annotated[str, typer.Argument(help="What to ask.")],
    cap: Annotated[str, typer.Option(help="Capability to invoke.")] = "explain",
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the reply.")] = False,
    timeout: Annotated[int, typer.Option(help="Seconds to wait with --wait.")] = 120,
    as_json: JsonOpt = False,
) -> None:
    """Send a request as the human; the quickest way to test an agent."""
    payload = call(
        as_json,
        "POST",
        "/v1/requests",
        json={"to": agent, "capability": cap, "task": task},
    )
    req = payload["request"]
    if wait and req["status"] not in FINAL:
        wait_s = max(1, min(timeout, 600))
        payload = call(
            as_json,
            "GET",
            f"/v1/requests/{req['request_id']}",
            timeout=wait_s + 10,
            params={"wait": wait_s},
        )
        req = payload["request"]
    text = f"{req['request_id']}  {req['status']}"
    if req.get("result"):
        text += f"\n{safe(req['result'])}"
    out(as_json, payload, text)
    if wait and req["status"] != "done":
        hint = (
            f"Still {req['status']}. Check `covey inbox {agent}` or `covey log`, "
            "or raise --timeout."
        )
        if not as_json:
            typer.echo(hint, err=True)
        raise typer.Exit(1)


@app.command()
def approvals(as_json: JsonOpt = False) -> None:
    """List requests waiting for your approval."""
    payload = call(as_json, "GET", "/v1/approvals")
    text = format_requests(payload["requests"], "No approvals pending.") + (
        "\nRun `covey approve <id>` or `covey deny <id>`."
        if payload["requests"]
        else ""
    )
    out(as_json, payload, text)


def pick_scope(
    as_json: bool, once: bool, session: bool, always: bool, default: str | None
) -> str:
    chosen = [
        n for n, on in (("once", once), ("session", session), ("always", always)) if on
    ]
    if len(chosen) > 1:
        raise fail(as_json, "Pick one of --once, --session, --always.")
    if chosen:
        return chosen[0]
    if default is None:
        raise fail(as_json, "Pick a scope: --once, --session or --always.")
    return default


Once = Annotated[bool, typer.Option("--once", help="One request only.")]
Session = Annotated[
    bool, typer.Option("--session", help="Until the target's session ends.")
]
Always = Annotated[bool, typer.Option("--always", help="Until revoked.")]


@app.command()
def approve(
    request_id: Annotated[
        str, typer.Argument(help="Request id from `covey approvals`.")
    ],
    once: Once = False,
    session: Session = False,
    always: Always = False,
    as_json: JsonOpt = False,
) -> None:
    """Approve a pending request (scope defaults to --once)."""
    scope = pick_scope(as_json, once, session, always, "once")
    payload = call(
        as_json, "POST", f"/v1/approvals/{request_id}", json={"scope": scope}
    )
    req = payload["request"]
    what = safe(
        f"{slug(req['from_agent'])} -> {slug(req['to_agent'])} {req['capability']}"
    )
    out(
        as_json,
        payload,
        f"Approved ({scope}): {what}: {safe(req['task'], 80)}\n"
        f"Request is {req['status']}.",
    )


@app.command()
def deny(
    request_id: Annotated[
        str, typer.Argument(help="Request id from `covey approvals`.")
    ],
    as_json: JsonOpt = False,
) -> None:
    """Deny a pending request."""
    payload = call(
        as_json, "POST", f"/v1/approvals/{request_id}", json={"scope": "deny"}
    )
    out(as_json, payload, f"Denied. Request is {payload['request']['status']}.")


@app.command()
def grants(as_json: JsonOpt = False) -> None:
    """List grants."""
    payload = call(as_json, "GET", "/v1/grants")
    rows = [
        [
            g["grant_id"],
            f"{slug(g['requester'])} -> {slug(g['target'])}",
            g["capability"],
            g["scope"],
        ]
        for g in payload["grants"]
    ]
    out(as_json, payload, table(rows, "No grants."))


@app.command()
def grant(
    requester: Annotated[str, typer.Argument(help="Requester slug.")],
    target: Annotated[str, typer.Argument(help="Target slug.")],
    capability: Annotated[str, typer.Argument(help="Capability name.")],
    once: Once = False,
    session: Session = False,
    always: Always = False,
    as_json: JsonOpt = False,
) -> None:
    """Let one agent use a capability of another."""
    scope = pick_scope(as_json, once, session, always, None)
    body = {
        "requester": requester,
        "target": target,
        "capability": capability,
        "scope": scope,
    }
    payload = call(as_json, "POST", "/v1/grants", json=body)
    g = payload["grant"]
    out(as_json, payload, f"Granted {capability} ({scope}) as {g['grant_id']}.")


@app.command()
def revoke(
    grant_id: Annotated[str, typer.Argument(help="Grant id from `covey grants`.")],
    as_json: JsonOpt = False,
) -> None:
    """Revoke a grant."""
    try:
        payload = call(as_json, "DELETE", f"/v1/grants/{grant_id}")
    except typer.Exit:
        # call() already printed the daemon's message; add the next step for a bad id.
        if not as_json:
            typer.echo("Run `covey grants` to see the current grant ids.", err=True)
        raise
    out(as_json, payload, f"Revoked {grant_id}.")


def check(name: str, ok: bool, detail: str, fix: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix}


def token_check(cfg: Any) -> dict[str, Any]:
    problems = []
    for p in (cfg.token_path, cfg.join_token_path):
        if not p.exists():
            problems.append(f"{p} is missing")
        elif p.stat().st_mode & 0o077:
            problems.append(f"{p} is mode {p.stat().st_mode & 0o777:o}, not 600")
    fix = "Run `covey up` to create them; for loose modes run " + " ".join(
        f"`chmod 600 {p}`" for p in (cfg.token_path, cfg.join_token_path)
    )
    return check(
        "tokens", not problems, "; ".join(problems) or "present, mode 600", fix
    )


def json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def install_checks() -> list[dict[str, Any]]:
    """Look where `covey init` writes: project files in the cwd, and the user's files."""
    home, cwd = Path.home(), Path.cwd()
    settings = [cwd / ".claude" / "settings.json", home / ".claude" / "settings.json"]
    mcp_files = [cwd / ".mcp.json", home / ".claude.json"]

    def hook_present(data: dict[str, Any], event: str, arg: str) -> bool:
        """A covey hook whose executable exists; an old bare `covey` off PATH is not one."""
        groups = data.get("hooks", {}).get(event, [])
        return any(
            is_covey_hook(h.get("command"), arg) and hook_runs(h["command"])
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        )

    loaded = [json_file(p) for p in settings]
    missing = [
        e
        for e, (arg, _) in HOOKS.items()
        if not any(hook_present(d, e, arg) for d in loaded)
    ]
    mcp_ok = any("covey" in json_file(p).get("mcpServers", {}) for p in mcp_files)
    return [
        check(
            "hooks",
            not missing,
            "all four hooks installed"
            if not missing
            else f"missing: {', '.join(missing)}",
            "Run `covey init` in your project (or `covey init --user`).",
        ),
        check(
            "mcp",
            mcp_ok,
            "mcpServers.covey found" if mcp_ok else "no mcpServers.covey entry",
            "Run `covey init` in your project (or `covey init --user`).",
        ),
    ]


# The Claude Code version the hook fixtures in tests/fixtures/hooks were recorded against.
TESTED_CLAUDE_CODE = (2, 1, 319)


def parse_claude_version(text: str) -> tuple[int, ...] | None:
    """`2.1.319 (Claude Code)` -> (2, 1, 319); None if no x.y.z is found."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in m.groups()) if m else None


def claude_version_check(output: str) -> dict[str, Any]:
    """Pass with a `warn` note (never a failure) when the version is older than the tested
    one, newer than it, or unreadable."""
    tested = ".".join(map(str, TESTED_CLAUDE_CODE))
    version = parse_claude_version(output)
    if version is None:
        c = check("claude", True, output or "(no output)")
        warn = (
            f"could not read a version from `claude --version`; Covey is tested with "
            f"{tested}. Update Claude Code, and report hook problems if any."
        )
    elif version < TESTED_CLAUDE_CODE:
        c = check("claude", True, output)
        warn = (
            f"older than the tested {tested}; hooks may not behave as recorded. "
            "Update Claude Code (`claude update`)."
        )
    elif version > TESTED_CLAUDE_CODE:
        c = check("claude", True, output)
        warn = f"newer than the tested {tested}; report hook problems."
    else:
        c = check("claude", True, output)
        warn = ""
    c["version"] = ".".join(map(str, version)) if version else None
    c["tested"] = version == TESTED_CLAUDE_CODE
    c["warn"] = warn
    return c


def claude_check() -> dict[str, Any]:
    fix = "Install Claude Code (https://docs.claude.com/claude-code) and put `claude` on PATH."
    exe = shutil.which("claude")
    if exe is None:
        return check("claude", False, "`claude` not found on PATH", fix)
    try:
        res = subprocess.run(
            [exe, "--version"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("claude", False, f"`claude --version` failed: {exc}", fix)
    output = res.stdout.strip() or res.stderr.strip()
    if res.returncode != 0:
        return check("claude", False, output, fix)
    return claude_version_check(output)


def claude_for_coveyd_check(cfg: Any) -> dict[str, Any]:
    from covey.daemon.waker import find_claude

    fix = (
        "Set COVEY_CLAUDE to the full path of `claude` (`which claude`), then"
        " `covey down && covey up`."
    )
    exe = find_claude(cfg)
    if exe is None:
        return check(
            "claude for coveyd",
            False,
            f"{cfg.claude_bin!r} not found (the daemon's lookup, run from this shell's PATH)",
            fix,
        )
    return check(
        "claude for coveyd",
        True,
        f"{exe} (checked from this shell with the daemon's lookup; coveyd's own PATH may differ)",
        fix,
    )


def notify_check(cfg: Any) -> dict[str, Any]:
    from covey.daemon import notify

    name = notify.backend_name(cfg.notify)
    fix = (
        "Start coveyd from a shell where `powershell.exe` is on PATH (Windows interop on),"
        " or set COVEY_NOTIFY=log to only log, then `covey down && covey up`."
    )
    if name == "powershell" and shutil.which("powershell.exe") is None:
        return check(
            "notify backend",
            False,
            "powershell on WSL, but `powershell.exe` is not on PATH: toasts will silently log only",
            fix,
        )
    return check("notify backend", True, name, fix)


def port_check(port: int) -> dict[str, Any]:
    fix = f"Stop whatever uses port {port}, or set COVEY_PORT to a free port, then `covey up`."
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return check("port", True, f"{port} is free", fix)
        except OSError:
            pass
    try:  # any coveyd answers (401) with its version field, no token needed
        body = httpx.get(f"http://127.0.0.1:{port}/v1/whoami", timeout=2).json()
        if isinstance(body, dict) and "covey" in body:
            return check("port", True, f"{port} is served by coveyd", fix)
    except (httpx.HTTPError, ValueError):
        pass
    return check("port", False, f"{port} is in use by something else", fix)


@app.command()
def doctor(as_json: JsonOpt = False) -> None:
    """Check the daemon, tokens, hooks, Claude Code and the port; print a fix for each failure."""
    cfg = load_cfg()
    running = daemon_answers(cfg)
    _, refusal = probe(cfg)
    checks = [
        check(
            "socket",
            running,
            f"coveyd answers on {cfg.socket_path}"
            if running
            else f"no coveyd on {cfg.socket_path}",
            "Run `covey up`.",
        ),
        token_check(cfg),
        check(
            "human token",
            refusal is None,
            refusal.get("message", "refused")
            if refusal
            else ("accepted" if running else "not checked: coveyd is not running"),
            (refusal or {}).get("next_step", ""),
        ),
        *install_checks(),
        claude_check(),
        claude_for_coveyd_check(cfg),
        notify_check(cfg),
        port_check(cfg.port),
    ]
    ok = all(c["ok"] for c in checks)
    lines = []
    for c in checks:
        lines.append(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
        if c.get("warn"):
            lines.append(f"     warn: {c['warn']}")
        if not c["ok"]:
            lines.append(f"     fix: {c['fix']}")
    out(as_json, {"ok": ok, "checks": checks}, "\n".join(lines))
    if not ok:
        raise typer.Exit(1)
