"""`covey hook <event>`: the Claude Code hook entry points.

Claude Code runs these on every session event, so they import only what they need and never
fail the session: any internal error is one line on stderr and exit 0.
"""

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from covey import config as config_module
from covey.config import Config

hook_app = typer.Typer(
    help="Claude Code hook entry points (configured by `covey init`, not run by hand).",
    no_args_is_help=True,
)

JsonOpt = Annotated[
    bool,
    typer.Option("--json", help="Print a JSON summary instead of the hook output."),
]


def _payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _context(cfg: Config, payload: dict[str, Any], *, starting: bool = False):
    """The HookContext for this session, or None when the hook should stay silent."""
    from covey.hooks.client import HookClient, HookContext
    from covey.mcp.identity import IdentityError, derive_slug, read_session

    info = read_session(cfg, os.environ)
    if info is not None:
        return HookContext(
            info["slug"], HookClient(cfg, info["agent_token"]), cfg, os.environ
        )
    if not starting:
        return None
    # SessionStart at startup runs before the shim has written its file.
    try:
        slug = derive_slug(os.environ, Path(payload.get("cwd") or os.getcwd()))
    except IdentityError:
        return None
    return HookContext(slug, None, cfg, os.environ)


def _run(event: str, as_json: bool, *, starting: bool = False) -> None:
    import importlib

    try:
        cfg = config_module.load()
        payload = _payload()
        ctx = _context(cfg, payload, starting=starting)
        output = None
        if ctx is not None:
            module = importlib.import_module(f"covey.hooks.{event.replace('-', '_')}")
            output = module.run(payload, ctx)
        if as_json:
            typer.echo(
                json.dumps({"event": event, "acted": ctx is not None, "output": output})
            )
        elif output is not None:
            typer.echo(json.dumps(output))
    except Exception as exc:  # noqa: BLE001 (a hook must never fail the session)
        print(f"covey hook {event}: {exc}", file=sys.stderr)
    raise typer.Exit(0)


@hook_app.command("session-start")
def session_start(as_json: JsonOpt = False) -> None:
    """SessionStart: report the session id and tell the model its name."""
    _run("session-start", as_json, starting=True)


@hook_app.command()
def stop(as_json: JsonOpt = False) -> None:
    """Stop: hand the model pending requests instead of ending the turn."""
    _run("stop", as_json)


@hook_app.command("post-tool")
def post_tool(as_json: JsonOpt = False) -> None:
    """PostToolUse: note requests waiting."""
    _run("post-tool", as_json)


@hook_app.command("session-end")
def session_end(as_json: JsonOpt = False) -> None:
    """SessionEnd: detach the session."""
    _run("session-end", as_json)
