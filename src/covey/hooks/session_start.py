"""SessionStart: report the new session id, and nudge the model to describe itself unless
its card is already filled in."""

import contextlib
from typing import Any

from covey.hooks.client import HookContext
from covey.mcp.identity import write_wake_file
from covey.procs import _start_time

NUDGE = (
    "You are agent `{slug}` in the covey. Call `covey_describe` once you know what this "
    "repo does. Other agents may ask you for help."
)

CARD_SET = (
    "You are agent `{slug}` in the covey; your card says: {summary!r}. Other "
    "agents may ask you for help."
)


def _leave_wake_socket(ctx: HookContext) -> str | None:
    """Claude Code's messaging socket path, which only hooks see. Saved for the shim (which
    may not exist yet) and returned for the daemon. Failing to save it is not an error."""
    socket = ctx.env.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not socket:
        return None
    with contextlib.suppress(Exception):
        pid = int(ctx.env["CLAUDE_PID"])
        start = _start_time(pid)
        if ctx.cfg is not None and start is not None:
            write_wake_file(ctx.cfg, pid, start, socket)
    return socket


def _context_line(ctx: HookContext) -> str:
    """The short line when the card has a summary and a capability, else the nudge. Any
    failure reading the card falls back to the nudge: a hook never breaks session start."""
    if ctx.client is not None:
        try:
            card = ctx.client.call("GET", "/v1/whoami")["card"]
        except Exception:  # noqa: BLE001
            card = None
        if card and card.get("summary", "").strip() and card.get("capabilities"):
            return CARD_SET.format(
                slug=ctx.slug, summary=" ".join(card["summary"].split())
            )
    return NUDGE.format(slug=ctx.slug)


def run(payload: dict[str, Any], ctx: HookContext) -> dict[str, Any]:
    """`ctx.client` is None at startup, when the shim has not written its file yet: the
    nudge still goes out, with the slug the shim will derive."""
    socket = _leave_wake_socket(ctx)
    if ctx.client is not None:
        session_id = payload.get("session_id")
        if session_id:
            body = {"session_id": session_id}
            if socket and len(socket) <= 200:
                body["wake_socket"] = socket
            ctx.client.call("PUT", f"/v1/agents/{ctx.slug}/session", json=body)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _context_line(ctx),
        }
    }
