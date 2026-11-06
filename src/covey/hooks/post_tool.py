"""PostToolUse: tell a busy model that requests are waiting, without interrupting it."""

from typing import Any

from covey.hooks.client import HookContext


def run(payload: dict[str, Any], ctx: HookContext) -> dict[str, Any] | None:
    got = ctx.client.call(
        "GET", f"/v1/agents/{ctx.slug}/inbox", params={"peek": "true"}
    )
    # Only requests nobody has seen: `delivered` ones were already handed to the model.
    waiting = [r for r in got.get("requests", []) if r.get("status") == "queued"]
    if not waiting:
        return None
    senders = list(dict.fromkeys(r["from_agent"].partition("@")[0] for r in waiting))
    n = len(waiting)
    note = (
        f"covey: {n} request{'s' if n != 1 else ''} waiting from "
        f"{', '.join(f'`{s}`' for s in senders)}, "
        "pick them up when you reach a stopping point"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": note,
        }
    }
