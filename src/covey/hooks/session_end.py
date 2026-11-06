"""SessionEnd: detach the session, except for /clear, which starts another one at once."""

from typing import Any

from covey.hooks.client import HookContext


def run(payload: dict[str, Any], ctx: HookContext) -> None:
    reason = str(payload.get("reason", ""))
    if reason == "clear":
        return
    ctx.client.call("DELETE", f"/v1/agents/{ctx.slug}/session", json={"reason": reason})
