"""`covey init`: merge the four hooks and the MCP server entry into Claude Code settings.

Formats confirmed against Claude Code 2.1.319: hooks live in `settings.json` as
`{"hooks": {Event: [{"matcher"?: str, "hooks": [{"type": "command", "command": str}]}]}}`;
`claude mcp add -s project` writes `.mcp.json` as
`{"mcpServers": {name: {"type": "stdio", "command": str, "args": [...], "env": {}}}}`;
user-scope MCP servers live under the same key in `~/.claude.json`.

Hook and MCP commands carry the absolute path of the `covey` executable that ran init, since
Claude Code starts them without the venv or tool directory on PATH. Under `uvx` that
executable lives in uv's throwaway cache (`<cache>/archive-v0/<hash>/bin/covey`) and
disappears on a cache clean, so init refuses there and says to `uv tool install covey-mesh`.
Re-running init is idempotent: it recognises a covey hook by its `hook <name>` tail and
rewrites an old bare `covey hook ...` command in place instead of adding a second one.
"""

import itertools
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

HOOKS: dict[str, tuple[str, str | None]] = {
    # event: (subcommand argument, matcher)
    "SessionStart": ("session-start", None),
    "Stop": ("stop", None),
    "PostToolUse": ("post-tool", "*"),
    "SessionEnd": ("session-end", None),
}
UVX_REFUSAL = (
    "`covey init` is running from uvx's temporary environment ({exe}), which uv can delete. "
    "Run `uv tool install covey-mesh && {init}` instead."
)


class InitError(Exception):
    pass


def hook_command(exe: str, arg: str) -> str:
    return f"{shlex.quote(exe)} hook {arg}"


def mcp_entry(exe: str) -> dict[str, Any]:
    return {"type": "stdio", "command": exe, "args": ["mcp"], "env": {}}


def is_covey_hook(command: object, arg: str) -> bool:
    """True for `<anything named covey> hook <arg>`, bare or absolute."""
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return (
        len(words) == 3
        and Path(words[0]).name == "covey"
        and words[1:] == ["hook", arg]
    )


def hook_runs(command: str) -> bool:
    """True when the command's executable exists (bare names are looked up on PATH)."""
    try:
        return shutil.which(shlex.split(command)[0]) is not None
    except (ValueError, IndexError):
        return False


def is_ephemeral(exe: Path) -> bool:
    """True when `exe` is inside uv's cache, where `uvx` unpacks its throwaway environments."""
    for path in (exe, exe.resolve()):
        cache = os.environ.get("UV_CACHE_DIR")
        if cache and path.is_relative_to(Path(cache).resolve()):
            return True
        parts = path.parts
        if any(
            parent == "uv" and child.startswith("archive-v")
            for parent, child in itertools.pairwise(parts)
        ):
            return True
    return False


def covey_executable(init: str = "covey init") -> str:
    """Absolute path of the `covey` that is running: the console script in argv[0], else
    `covey` on PATH, else next to this Python. Raises InitError when none is a real file;
    its next step repeats `init`, the command the user ran."""
    argv0 = Path(sys.argv[0])
    candidates = [argv0.absolute() if argv0.name == "covey" else None]
    found = shutil.which("covey")
    candidates += [
        Path(found).absolute() if found else None,
        Path(sys.executable).absolute().parent / "covey",
    ]
    for path in candidates:
        if path is not None and path.is_file():
            if is_ephemeral(path):
                raise InitError(UVX_REFUSAL.format(exe=path, init=init))
            return str(path)
    raise InitError(
        "Could not find the `covey` executable to write into the hooks. "
        f"Run `uv tool install covey-mesh && {init}`."
    )


def hooks_available() -> bool:
    """True once the CLI has a `hook` command. Until then, installing hooks would make
    Claude Code run `covey hook ...` and get exit 2, which blocks Stop."""
    from typer.main import get_command

    from covey.cli import app

    return "hook" in getattr(get_command(app), "commands", {})


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


def merge_hooks(settings: dict[str, Any], exe: str) -> list[tuple[str, str]]:
    """Add or update covey hooks in place; return (event, "added" | "updated") pairs."""
    changed = []
    hooks = object_key(settings, "hooks")
    for event, (arg, matcher) in HOOKS.items():
        command = hook_command(exe, arg)
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise InitError(
                f'The "hooks.{event}" key must be a list. Fix it, then run `covey init` again.'
            )
        entries = [
            h
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict) and is_covey_hook(h.get("command"), arg)
        ]
        if any(h["command"] == command for h in entries):
            continue
        if entries:  # an old bare or moved command: rewrite it in place
            for h in entries:
                h["command"] = command
            changed.append((event, "updated"))
            continue
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        changed.append((event, "added"))
    return changed


def merge_mcp(config: dict[str, Any], exe: str) -> str | None:
    """Return "added", "updated" (an old bare `covey` command) or None (left alone)."""
    servers = object_key(config, "mcpServers")
    current = servers.get("covey")
    if current is None:
        servers["covey"] = mcp_entry(exe)
        return "added"
    if isinstance(current, dict) and current.get("command") == "covey":
        current["command"] = exe
        return "updated"
    return None


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
    exe = covey_executable("covey init" if home is None else "covey init --user")
    settings, mcp = read_json(settings_path), read_json(mcp_path)
    done = merge_hooks(settings, exe) if hooks_available() else []
    changes = [
        {
            "file": str(settings_path),
            "action": action,
            "added": f"{e} hook ({hook_command(exe, HOOKS[e][0])})",
        }
        for e, action in done
    ]
    hooks_changed = bool(changes)
    mcp_action = merge_mcp(mcp, exe)
    if mcp_action:
        changes.append(
            {
                "file": str(mcp_path),
                "action": mcp_action,
                "added": f"mcpServers.covey ({exe} mcp)",
            }
        )
    if hooks_changed:
        write_json(settings_path, settings)
    if mcp_action:
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
        lines = [f"{c['file']}: {c['action']} {c['added']}" for c in changes]
        typer.echo(
            "\n".join(lines) + "\nRestart Claude Code sessions, then run `covey up`."
        )
