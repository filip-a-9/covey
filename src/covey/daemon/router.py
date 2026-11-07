"""Router: requests, threads, hops, statuses, deadlines, approvals.

Every function takes an open connection and joins the caller's transaction if there is one
(`store.transaction`). Every status change writes an audit row. Waiters are notified by the
app after each mutating call, not from here: this module stays synchronous and pure-SQL.

Errors are raised as `CoveyError(status, code, message, next_step)`; every message says what
to do next. Request dicts returned from here are shaped like `Request` in api.yaml
(`artifacts` decoded to a list, everything else as stored).

Decisions for this slice (docs/SPEC.md "Trust model" and "Delivery"):
- A capability the target never declared is treated as gated (needs a grant / approval).
- Requests to the human pseudo-agent (slug `human`) need no grant.
- A `gone` target is refused with 422 `target_unreachable`.
- `forbidden` -> created with status `refused`; a `deny` grant -> `refused`;
  `ask` -> `awaiting_grant`; `allow` -> `queued`.
- Reading the inbox moves `queued` to `delivered`. `working` means a headless worker the
  daemon spawned has claimed the request (waker.py); only the daemon sets it.
- A `once` grant is checked at creation and consumed at a successful final reply
  (done/failed). While one request holds it (open and not awaiting a grant), further
  matching requests land in `awaiting_grant`: one in flight per grant. Every reply to a non-open capability re-checks the grant first; if it no
  longer allows, the request becomes `failed` (result `grant_revoked`) and the reply is
  refused with 403 `grant_revoked`.
- At its deadline an open request becomes `expired`; result `awaiting_grant_timeout` if it
  was still awaiting a grant, else `deadline_passed`.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from covey.daemon import grants, registry, store
from covey.daemon.errors import CoveyError
from covey.ids import new_ulid

FINAL_STATUSES = ("done", "failed", "refused", "expired")
INBOX_DEFAULT_STATUSES = ("queued", "delivered", "working", "needs_input")
REPLY_STATUSES = ("done", "failed", "needs_input")

_NOT_FOUND_NEXT = "Check the id, or see recent requests with `covey log`."


def _out(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d.pop("stop_blocked_at", None)
    d["artifacts"] = json.loads(d["artifacts"])
    return d


def _set_status(
    conn: sqlite3.Connection,
    request_id: str,
    status: str,
    result: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    """Update status (and result/artifacts when given). The caller audits."""
    conn.execute(
        "UPDATE requests SET status = ?, result = COALESCE(?, result),"
        " artifacts = COALESCE(?, artifacts), updated_at = ? WHERE request_id = ?",
        (
            status,
            result,
            None if artifacts is None else json.dumps(artifacts),
            store.now_iso(),
            request_id,
        ),
    )


def _level(agent: dict[str, Any], capability: str) -> str | None:
    for cap in registry.card_of(agent)["capabilities"]:
        if cap["name"] == capability:
            return cap["level"]
    return None


def _live_session(agent: dict[str, Any]) -> str | None:
    return agent["session_id"] if agent["liveness"] == "live" else None


def _agent_by_id(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    return registry.get_agent(conn, agent_id.split("@", 1)[0])


def _thread_rows(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE thread_id = ?", (thread_id,)
    ).fetchall()


def _not_participant() -> CoveyError:
    return CoveyError(
        403,
        "not_a_participant",
        "You did not send or receive that request or thread.",
        "Use a request or thread you took part in (`covey log`).",
    )


def _place_in_thread(
    conn: sqlite3.Connection,
    from_agent: str,
    thread_id: str | None,
    parent_id: str | None,
) -> tuple[str | None, int, str | None]:
    """Resolve (thread_id, hops, parent_id); thread_id None means a new thread."""
    if parent_id:
        parent = get_request(conn, parent_id)
        if not is_participant(parent, from_agent):
            raise _not_participant()
        if thread_id and thread_id != parent["thread_id"]:
            raise CoveyError(
                400,
                "thread_mismatch",
                "thread_id does not match the parent request's thread.",
                "Drop thread_id; it is inherited from parent_id.",
            )
        return parent["thread_id"], parent["hops"] + 1, parent_id
    if thread_id:
        rows = _thread_rows(conn, thread_id)
        if not rows:
            raise CoveyError(
                404,
                "thread_not_found",
                f"No thread {thread_id}.",
                "Omit thread_id to start a new thread.",
            )
        if not any(from_agent in (r["from_agent"], r["to_agent"]) for r in rows):
            raise _not_participant()
        return thread_id, max(r["hops"] for r in rows) + 1, None
    return None, 0, None


_CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def create_request(
    conn: sqlite3.Connection,
    from_agent: str,
    to: str,
    capability: str,
    task: str,
    context: str | None = None,
    thread_id: str | None = None,
    parent_id: str | None = None,
    timeout_s: int | None = None,
    max_hops: int = 4,
) -> dict[str, Any]:
    """Create a request from `from_agent` (an agent_id) to the agent with slug `to`.

    - Unknown slug: 404 `agent_not_found`.
    - `parent_id`: must exist (404 `request_not_found`) and `from_agent` must have sent or
      received it (403 `not_a_participant`). Thread is inherited, hops = parent.hops + 1.
      A `thread_id` that disagrees with the parent's thread: 400 `thread_mismatch`.
    - `thread_id` without parent: the thread must exist (404 `thread_not_found`) and
      `from_agent` must be a participant (403); hops = max hops in the thread + 1.
    - Neither: a new thread whose id is the request id, hops 0.
    - hops > max_hops: 422 `hops_exceeded`.
    - `timeout_s` sets `deadline` = now + timeout_s.
    Audits `request.created` (and `approval.requested` when it lands in awaiting_grant).
    """
    if not _CAPABILITY_NAME.fullmatch(capability):
        raise CoveyError(
            400,
            "bad_capability",
            f"{capability!r} is not a capability name.",
            "Use a capability the target declares in its card, e.g. `explain`; "
            "`*` exists only in grants the human makes.",
        )
    with store.transaction(conn):
        target = registry.get_agent(conn, to)
        thread, hops, parent = _place_in_thread(conn, from_agent, thread_id, parent_id)
        if hops > max_hops:
            raise CoveyError(
                422,
                "hops_exceeded",
                f"This request would be hop {hops}; the limit is {max_hops}.",
                "Answer from what you have, or ask the human instead of delegating further.",
            )
        if target["liveness"] == "gone":
            raise CoveyError(
                422,
                "target_unreachable",
                f"{to!r} is gone: it has no session and cannot be woken.",
                "Pick another agent (`covey find`) or ask the human.",
            )
        now = store.now_iso()
        result = None
        if target["slug"] == "human":
            status = "queued"
        else:
            decision = grants.decide(
                _level(target, capability),
                capability,
                grants.matching_grants(
                    conn, from_agent, target["agent_id"], capability
                ),
                _live_session(target),
                now,
            )
            status = {"allow": "queued", "ask": "awaiting_grant", "deny": "refused"}[
                decision.outcome
            ]
            if (
                status == "queued"
                and decision.grant is not None
                and decision.grant["scope"] == "once"
                and _has_open_request(
                    conn, from_agent, target["agent_id"], decision.grant["capability"]
                )
            ):
                status = "awaiting_grant"
            if status == "refused":
                result = "capability_refused"
        request_id = new_ulid()
        row = {
            "request_id": request_id,
            "thread_id": thread or request_id,
            "parent_id": parent,
            "from_agent": from_agent,
            "to_agent": target["agent_id"],
            "capability": capability,
            "task": task,
            "context": context,
            "status": status,
            "result": result,
            "hops": hops,
            "created_at": now,
            "updated_at": now,
            "deadline": _deadline(timeout_s),
        }
        conn.execute(
            "INSERT INTO requests (request_id, thread_id, parent_id, from_agent, to_agent,"
            " capability, task, context, status, result, hops, created_at, updated_at,"
            " deadline) VALUES (:request_id, :thread_id, :parent_id, :from_agent, :to_agent,"
            " :capability, :task, :context, :status, :result, :hops, :created_at,"
            " :updated_at, :deadline)",
            row,
        )
        detail = {"capability": capability, "to": to, "status": status}
        store.audit(conn, from_agent, "request.created", request_id, detail)
        if status == "awaiting_grant":
            store.audit(conn, from_agent, "approval.requested", request_id, detail)
        return get_request(conn, request_id)


def _has_open_request(
    conn: sqlite3.Connection, from_agent: str, to_agent: str, capability: str
) -> bool:
    """True if a request already holds the `once` grant; a `*` grant covers every capability."""
    row = conn.execute(
        "SELECT 1 FROM requests WHERE from_agent = ? AND to_agent = ?"
        " AND (? = '*' OR capability = ?)"
        " AND status IN ('queued', 'delivered', 'working', 'needs_input') LIMIT 1",
        (from_agent, to_agent, capability, capability),
    ).fetchone()
    return row is not None


def _deadline(timeout_s: int | None) -> str | None:
    if timeout_s is None:
        return None
    at = datetime.now(UTC) + timedelta(seconds=timeout_s)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_request(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    """Return one request, or raise 404 `request_not_found`."""
    row = conn.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise CoveyError(
            404, "request_not_found", f"No request {request_id}.", _NOT_FOUND_NEXT
        )
    return _out(row)


def is_participant(request: dict[str, Any], agent_id: str) -> bool:
    """True if `agent_id` sent or received the request."""
    return agent_id in (request["from_agent"], request["to_agent"])


def inbox(
    conn: sqlite3.Connection,
    agent_id: str,
    status: str | None = None,
    deliver: bool = True,
) -> list[dict[str, Any]]:
    """Requests addressed to `agent_id`, oldest first (created_at, request_id).

    Default statuses: INBOX_DEFAULT_STATUSES. With `deliver` (the target itself reading),
    every returned `queued` request moves to `delivered` (audited `request.delivered`) and
    is returned with the new status; anyone else only peeks. Requests still awaiting a grant
    never appear: they reach the target only after the human approves (400 `bad_status`).
    """
    if status == "awaiting_grant":
        raise CoveyError(
            400,
            "bad_status",
            "Requests awaiting a grant are not in the target's inbox until the human approves.",
            "The human sees them with `covey approvals` or GET /v1/approvals.",
        )
    statuses = (status,) if status else INBOX_DEFAULT_STATUSES
    marks = ",".join("?" * len(statuses))
    with store.transaction(conn):
        rows = conn.execute(
            f"SELECT * FROM requests WHERE to_agent = ? AND status IN ({marks})"
            " ORDER BY created_at, request_id",
            (agent_id, *statuses),
        ).fetchall()
        for row in rows:
            if deliver and row["status"] == "queued":
                _set_status(conn, row["request_id"], "delivered")
                store.audit(conn, agent_id, "request.delivered", row["request_id"])
        return [get_request(conn, r["request_id"]) for r in rows]


def stop_claim(conn: sqlite3.Connection, agent_id: str) -> list[dict[str, Any]]:
    """`queued` requests for `agent_id` not yet handed to a Stop hook, oldest first.

    Only `queued`: anything later (delivered, working, needs_input) was already seen by the
    model. Stamps `stop_blocked_at` so the hook blocks at most once per claim, and leaves
    the status `queued`: the model's own inbox read delivers it, as for an idle wake. If
    the hook dies before printing the block, `release_wake_claims` offers it again.
    """
    with store.transaction(conn):
        rows = conn.execute(
            "SELECT * FROM requests WHERE to_agent = ? AND stop_blocked_at IS NULL"
            " AND status = 'queued' ORDER BY created_at, request_id",
            (agent_id,),
        ).fetchall()
        now = store.now_iso()
        for row in rows:
            conn.execute(
                "UPDATE requests SET stop_blocked_at = ? WHERE request_id = ?",
                (now, row["request_id"]),
            )
        return [get_request(conn, r["request_id"]) for r in rows]


def wake_claim(conn: sqlite3.Connection, request_id: str) -> bool:
    """Stamp a `queued` request as posted to a session's wake socket. The status stays
    `queued`; the stamp keeps `stop_claim` and the waker off it. False if already stamped
    or no longer queued."""
    cur = conn.execute(
        "UPDATE requests SET stop_blocked_at = ? WHERE request_id = ?"
        " AND status = 'queued' AND stop_blocked_at IS NULL",
        (store.now_iso(), request_id),
    )
    return cur.rowcount == 1


def release_wake_claim(conn: sqlite3.Connection, request_id: str) -> bool:
    """Clear the wake stamp on one request that is still `queued` (the post failed, or the
    window ran out), so the hooks deliver it. Audits `request.wake_released`."""
    with store.transaction(conn):
        row = conn.execute(
            "SELECT to_agent FROM requests WHERE request_id = ? AND status = 'queued'"
            " AND stop_blocked_at IS NOT NULL",
            (request_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE requests SET stop_blocked_at = NULL WHERE request_id = ?",
            (request_id,),
        )
        node = row["to_agent"].split("@", 1)[1]
        store.audit(conn, store.daemon_actor(node), "request.wake_released", request_id)
    return True


def release_wake_claims(conn: sqlite3.Connection, cutoff: str) -> list[str]:
    """Release stamps older than `cutoff` on requests still `queued`: the idle wake or Stop
    hook block was never acked by a `covey_inbox` read. Returns the released ids."""
    rows = conn.execute(
        "SELECT request_id FROM requests WHERE status = 'queued'"
        " AND stop_blocked_at < ? ORDER BY created_at, request_id",
        (cutoff,),
    ).fetchall()
    return [r["request_id"] for r in rows if release_wake_claim(conn, r["request_id"])]


def reply(
    conn: sqlite3.Connection,
    request_id: str,
    replier: str,
    result: str,
    status: str,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store a reply.

    - Only the target may reply: 403 `not_target`.
    - `awaiting_grant`: 409 `awaiting_grant`.
    - Already final: an identical (result, status, artifacts) reply returns the stored
      request unchanged; anything else is 409 `already_finished`.
    - Grant re-check as described in the module docstring; consume a `once` grant on a
      successful done/failed reply.
    Audits `request.replied`.
    """
    artifacts = artifacts or []
    revoked = False
    with store.transaction(conn):
        req = get_request(conn, request_id)
        if replier != req["to_agent"]:
            raise CoveyError(
                403,
                "not_target",
                "Only the agent the request was addressed to can reply.",
                "Check `covey log` for who the request went to.",
            )
        if status not in REPLY_STATUSES:
            raise CoveyError(
                400,
                "bad_status",
                f"Reply status {status!r} is not allowed.",
                f"Use one of {REPLY_STATUSES}.",
            )
        if req["status"] == "awaiting_grant":
            raise CoveyError(
                409,
                "awaiting_grant",
                "The human has not approved this request yet.",
                "Wait; ask the human to run `covey approve`.",
            )
        if req["status"] in FINAL_STATUSES:
            same = (req["result"], req["status"], req["artifacts"]) == (
                result,
                status,
                artifacts,
            )
            if same:
                return req
            raise CoveyError(
                409,
                "already_finished",
                f"The request is already {req['status']}.",
                "Send a new request if more work is needed.",
            )
        used = None
        target = _agent_by_id(conn, req["to_agent"])
        level = _level(target, req["capability"])
        if target["slug"] != "human" and level != "open":
            decision = grants.decide(
                level,
                req["capability"],
                grants.matching_grants(
                    conn, req["from_agent"], req["to_agent"], req["capability"]
                ),
                _live_session(target),
                store.now_iso(),
            )
            if decision.outcome == "allow":
                used = decision.grant
            else:
                revoked = True
                _set_status(conn, request_id, "failed", "grant_revoked")
                store.audit(conn, replier, "request.grant_revoked", request_id)
        if not revoked:
            _set_status(conn, request_id, status, result, artifacts)
            store.audit(
                conn, replier, "request.replied", request_id, {"status": status}
            )
            if used is not None and status in ("done", "failed"):
                grants.consume(conn, used, replier)
    if revoked:
        raise CoveyError(
            403,
            "grant_revoked",
            "The grant that allowed this request is gone, so the reply was not accepted.",
            "Do not act on the task; ask the human for a new grant if it is still wanted.",
        )
    return get_request(conn, request_id)


def claim_for_worker(conn: sqlite3.Connection, request_id: str, actor: str) -> bool:
    """`queued` -> `working` for a spawned worker; stamps `stop_blocked_at` so a session that
    attaches mid-run is never handed it by stop-claim. Audits `worker.spawned` (the spawn
    budget counts these rows). False if the request is no longer queued."""
    with store.transaction(conn):
        req = get_request(conn, request_id)
        if req["status"] != "queued":
            return False
        conn.execute(
            "UPDATE requests SET stop_blocked_at = ? WHERE request_id = ?",
            (store.now_iso(), request_id),
        )
        _set_status(conn, request_id, "working")
        store.audit(
            conn, actor, "worker.spawned", request_id, {"agent": req["to_agent"]}
        )
    return True


def fail_open(
    conn: sqlite3.Connection,
    request_id: str,
    code: str,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    """A worker could not serve the request: `queued`/`working` -> `failed` with `code` as
    the result, audited `worker.failed`. No grant is consumed. False if it had moved on."""
    with store.transaction(conn):
        req = get_request(conn, request_id)
        if req["status"] not in ("queued", "working"):
            return False
        _set_status(conn, request_id, "failed", code)
        store.audit(
            conn,
            actor,
            "worker.failed",
            request_id,
            {"agent": req["to_agent"], "code": code, **(detail or {})},
        )
    return True


def pending_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Requests in `awaiting_grant`, oldest first."""
    rows = conn.execute(
        "SELECT * FROM requests WHERE status = 'awaiting_grant'"
        " ORDER BY created_at, request_id"
    )
    return [_out(r) for r in rows]


def answer_approval(
    conn: sqlite3.Connection, request_id: str, scope: str, human: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Answer an approval as the human: create the grant, then move the request on.

    409 `not_awaiting_grant` unless the request is in `awaiting_grant`. A `session` scope
    ties the grant to the target's current `session_id` (only while the target is live;
    otherwise `grants.create_grant` raises 409). `deny` -> request `refused`; any other
    scope -> `queued`. Audits `approval.answered`. Returns (request, grant).
    """
    with store.transaction(conn):
        req = get_request(conn, request_id)
        if req["status"] != "awaiting_grant":
            raise CoveyError(
                409,
                "not_awaiting_grant",
                f"The request is {req['status']}, not waiting for approval.",
                "List what is waiting with `covey approve` (no id).",
            )
        target = _agent_by_id(conn, req["to_agent"])
        grant = grants.create_grant(
            conn,
            req["from_agent"],
            req["to_agent"],
            req["capability"],
            scope,
            human,
            session_id=_live_session(target),
        )
        if scope == "deny":
            _set_status(conn, request_id, "refused", "denied_by_human")
        else:
            _set_status(conn, request_id, "queued")
        store.audit(
            conn,
            human,
            "approval.answered",
            request_id,
            {"scope": scope, "grant_id": grant["grant_id"]},
        )
        return get_request(conn, request_id), grant


def expire_due(conn: sqlite3.Connection, now: str) -> list[str]:
    """Expire open requests whose deadline <= now. Returns the expired request ids."""
    final = ",".join("?" * len(FINAL_STATUSES))
    with store.transaction(conn):
        rows = conn.execute(
            "SELECT * FROM requests WHERE deadline IS NOT NULL AND deadline <= ?"
            f" AND status NOT IN ({final}) ORDER BY deadline, request_id",
            (now, *FINAL_STATUSES),
        ).fetchall()
        for row in rows:
            reason = (
                "awaiting_grant_timeout"
                if row["status"] == "awaiting_grant"
                else "deadline_passed"
            )
            _set_status(conn, row["request_id"], "expired", reason)
            node = row["to_agent"].split("@", 1)[1]
            store.audit(
                conn,
                store.daemon_actor(node),
                "request.expired",
                row["request_id"],
                {"reason": reason},
            )
    return [r["request_id"] for r in rows]


def log(
    conn: sqlite3.Connection,
    thread: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    limit: int = 50,
    visible_to: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Requests and audit events for `covey log`: {"requests": [...], "events": [...]}.

    `agent` is a slug or agent_id: requests it sent or received, events it acted in or that
    are about its requests. `since` filters on created_at / ts. The newest `limit` of each,
    shown oldest first. `visible_to` (an agent_id) restricts both lists to threads that agent
    took part in. Events are shaped like AuditEvent without `covey` (the app adds it), with
    `detail` decoded.
    """
    req_where: list[str] = []
    req_args: list[Any] = []
    ev_where: list[str] = []
    ev_args: list[Any] = []
    if thread:
        req_where.append("thread_id = ?")
        req_args.append(thread)
        ev_where.append(
            "subject IN (SELECT request_id FROM requests WHERE thread_id = ?)"
        )
        ev_args.append(thread)
    if agent:
        agent_id = (
            agent if "@" in agent else registry.get_agent(conn, agent)["agent_id"]
        )
        req_where.append("(from_agent = ? OR to_agent = ?)")
        req_args += [agent_id, agent_id]
        ev_where.append(
            "(actor = ? OR subject IN (SELECT request_id FROM requests"
            " WHERE from_agent = ? OR to_agent = ?))"
        )
        ev_args += [agent_id] * 3
    if visible_to:
        threads = "SELECT thread_id FROM requests WHERE from_agent = ? OR to_agent = ?"
        req_where.append(f"thread_id IN ({threads})")
        ev_where.append(
            "subject IN (SELECT request_id FROM requests"
            f" WHERE thread_id IN ({threads}))"
        )
        req_args += [visible_to, visible_to]
        ev_args += [visible_to, visible_to]
    if since:
        req_where.append("created_at >= ?")
        req_args.append(since)
        ev_where.append("ts >= ?")
        ev_args.append(since)
    req_sql = "SELECT * FROM requests"
    ev_sql = "SELECT id, ts, actor, event, subject, detail FROM audit"
    if req_where:
        req_sql += " WHERE " + " AND ".join(req_where)
    if ev_where:
        ev_sql += " WHERE " + " AND ".join(ev_where)
    requests = conn.execute(
        req_sql + " ORDER BY created_at DESC, request_id DESC LIMIT ?",
        (*req_args, limit),
    ).fetchall()
    events = conn.execute(
        ev_sql + " ORDER BY id DESC LIMIT ?", (*ev_args, limit)
    ).fetchall()
    return {
        "requests": [_out(r) for r in reversed(requests)],
        "events": [
            {**dict(e), "detail": json.loads(e["detail"])} for e in reversed(events)
        ],
    }
