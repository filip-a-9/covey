"""Stop: hand the model any request addressed to it instead of letting the turn end."""

from typing import Any

from covey.frames import CALL_TO_ACTION, FRAME
from covey.hooks.client import HookContext

TASK_LIMIT = 500


def describe(request: dict[str, Any]) -> str:
    task = " ".join(str(request.get("task", "")).split())
    if len(task) > TASK_LIMIT:
        task = task[:TASK_LIMIT] + "..."
    who = str(request.get("from_agent", "")).partition("@")[0] or "unknown"
    return f"- From `{who}` ({request.get('capability', '?')}): {task}"


def run(payload: dict[str, Any], ctx: HookContext) -> dict[str, Any] | None:
    # No early return on `stop_hook_active`: a request that arrived during the extended
    # turn is new, and the daemon's stamp already keeps each request from blocking twice.
    got = ctx.client.call("POST", f"/v1/agents/{ctx.slug}/inbox/stop-claim")
    requests = got.get("requests", [])
    if not requests:
        return None
    lines = [FRAME, *(describe(r) for r in requests), CALL_TO_ACTION]
    return {"decision": "block", "reason": "\n".join(lines)}
