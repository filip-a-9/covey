"""SessionStart: report the new session id and nudge the model to describe itself."""

from typing import Any

from covey.hooks.client import HookContext

NUDGE = (
    "You are agent `{slug}` in the covey. Call `covey_describe` once you know what this "
    "repo does. Other agents may ask you for help."
)


def run(payload: dict[str, Any], ctx: HookContext) -> dict[str, Any]:
    """`ctx.client` is None at startup, when the shim has not written its file yet: the
    nudge still goes out, with the slug the shim will derive."""
    if ctx.client is not None:
        session_id = payload.get("session_id")
        if session_id:
            ctx.client.call(
                "PUT", f"/v1/agents/{ctx.slug}/session", json={"session_id": session_id}
            )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": NUDGE.format(slug=ctx.slug),
        }
    }
