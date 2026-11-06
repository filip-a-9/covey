"""`covey init`: merge the four hooks and the MCP server entry into Claude Code settings.

Formats confirmed against Claude Code 2.1.319: hooks live in `settings.json` as
`{"hooks": {Event: [{"matcher"?: str, "hooks": [{"type": "command", "command": str}]}]}}`;
`claude mcp add -s project` writes `.mcp.json` as
`{"mcpServers": {name: {"type": "stdio", "command": str, "args": [...], "env": {}}}}`;
user-scope MCP servers live under the same key in `~/.claude.json`.
"""

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

HOOKS: dict[str, tuple[str, str | None]] = {
    # event: (command, matcher)
    "SessionStart": ("covey hook session-start", None),
    "Stop": ("covey hook stop", None),
    "PostToolUse": ("covey hook post-tool", "*"),
    "SessionEnd": ("covey hook session-end", None),
}
MCP_ENTRY: dict[str, Any] = {
    "type": "stdio",
    "command": "covey",
    "args": ["mcp"],
    "env": {},
}


def hooks_available() -> bool:
    """True once the CLI has a `hook` command. Until then, installing hooks would make
    Claude Code run `covey hook ...` and get exit 2, which blocks Stop."""
    from typer.main import get_command

    from covey.cli import app

    return "hook" in getattr(get_command(app), "commands", {})


class InitError(Exception):
    pass


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise InitError(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}). "
            "Fix or remove that file, then run `covey init` again."
        ) from exc
    if not isinstance(data, dict):
        raise InitError(
            f"{path} must hold a JSON object. Fix that file, then run `covey init` again."
        )
    return data


def object_key(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise InitError(
            f'The "{key}" key must be a JSON object. Fix it, then run `covey init` again.'
        )
    return value


def merge_hooks(settings: dict[str, Any]) -> list[str]:
    """Add missing covey hooks in place; return the events that were added."""
    added = []
    hooks = object_key(settings, "hooks")
    for event, (command, matcher) in HOOKS.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise InitError(
                f'The "hooks.{event}" key must be a list. Fix it, then run `covey init` again.'
            )
        present = any(
            h.get("command") == command
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        )
        if present:
            continue
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        added.append(event)
    return added


def merge_mcp(config: dict[str, Any]) -> bool:
    servers = object_key(config, "mcpServers")
    if "covey" in servers:
        return False
    servers["covey"] = dict(MCP_ENTRY)
    return True


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".covey-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def run_init(base: Path, home: Path | None) -> list[dict[str, str]]:
    """Apply the merge; `home` set means user-level. Nothing is written if any file errors."""
    if home is None:
        settings_path, mcp_path = base / ".claude" / "settings.json", base / ".mcp.json"
    else:
        settings_path, mcp_path = (
            home / ".claude" / "settings.json",
            home / ".claude.json",
        )
    settings, mcp = read_json(settings_path), read_json(mcp_path)
    added = merge_hooks(settings) if hooks_available() else []
    changes = [
        {"file": str(settings_path), "added": f"{e} hook ({HOOKS[e][0]})"}
        for e in added
    ]
    hooks_changed = bool(changes)
    mcp_changed = merge_mcp(mcp)
    if mcp_changed:
        changes.append({"file": str(mcp_path), "added": "mcpServers.covey (covey mcp)"})
    if hooks_changed:
        write_json(settings_path, settings)
    if mcp_changed:
        write_json(mcp_path, mcp)
    return changes


def init(
    user: Annotated[
        bool, typer.Option("--user", help="Write user-level files, not this project's.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Add the covey hooks and MCP server to Claude Code settings (idempotent)."""
    try:
        changes = run_init(Path.cwd(), Path.home() if user else None)
    except InitError as exc:
        typer.echo(
            json.dumps({"error": str(exc)}) if as_json else str(exc), err=not as_json
        )
        raise typer.Exit(1) from exc
    if as_json:
        scope = "user" if user else "project"
        typer.echo(
            json.dumps({"changed": bool(changes), "scope": scope, "changes": changes})
        )
    elif not changes:
        typer.echo(
            "Already set up: nothing changed. Run `covey up` to start the daemon."
        )
    else:
        lines = [f"{c['file']}: added {c['added']}" for c in changes]
        typer.echo(
            "\n".join(lines) + "\nRestart Claude Code sessions, then run `covey up`."
        )
