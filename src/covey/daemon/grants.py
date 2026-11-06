"""Grants: may this requester invoke this capability on this target?

`decide()` is pure. The helpers below it read and write the `grants` table; they join the
caller's transaction if there is one.

Rules (docs/SPEC.md, "Trust model"):
- `forbidden` is never lifted by any grant. `open` needs no grant.
- `gated`, and a capability the target never declared (level None), need a grant.
- A grant for the exact capability wins over a `*` grant. `*` counts only if the human made it.
- `session` grants match only the target's current live session; expired grants never match.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NamedTuple

from covey.daemon import store
from covey.daemon.errors import CoveyError
from covey.ids import new_ulid

Outcome = Literal["allow", "deny", "ask"]
SCOPES = ("once", "session", "always", "deny")


class Decision(NamedTuple):
    outcome: Outcome
    grant: Mapping[str, Any] | None


def _is_human(agent_id: str) -> bool:
    return agent_id.split("@", 1)[0] == "human"


def _usable(row: Mapping[str, Any], target_session: str | None, now: str) -> bool:
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return False
    if row["scope"] == "session":
        return target_session is not None and row["session_id"] == target_session
    return True


def decide(
    level: str | None,
    capability: str,
    grant_rows: Sequence[Mapping[str, Any]],
    target_session: str | None,
    now: str,
) -> Decision:
    """Decide a request. `target_session` is the target's session id while it is live, else None."""
    if level == "forbidden":
        return Decision("deny", None)
    if level == "open":
        return Decision("allow", None)
    usable = [r for r in grant_rows if _usable(r, target_session, now)]
    exact = [r for r in usable if r["capability"] == capability]
    wild = [r for r in usable if r["capability"] == "*" and _is_human(r["created_by"])]
    for row in exact or wild:
        return Decision("deny" if row["scope"] == "deny" else "allow", row)
    return Decision("ask", None)


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def matching_grants(
    conn: sqlite3.Connection, requester: str, target: str, capability: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM grants WHERE requester = ? AND target = ? AND capability IN (?, '*')",
        (requester, target, capability),
    )
    return [_as_dict(r) for r in rows]


def get_grant(conn: sqlite3.Connection, grant_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
    ).fetchone()
    return _as_dict(row) if row else None


def list_grants(
    conn: sqlite3.Connection,
    requester: str | None = None,
    target: str | None = None,
    involving: str | None = None,
) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM grants WHERE 1 = 1", []
    if requester:
        sql, args = sql + " AND requester = ?", [*args, requester]
    if target:
        sql, args = sql + " AND target = ?", [*args, target]
    if involving:
        sql, args = (
            sql + " AND (requester = ? OR target = ?)",
            [*args, involving, involving],
        )
    return [
        _as_dict(r) for r in conn.execute(sql + " ORDER BY created_at, grant_id", args)
    ]


def create_grant(
    conn: sqlite3.Connection,
    requester: str,
    target: str,
    capability: str,
    scope: str,
    created_by: str,
    session_id: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Create a grant, replacing any for the same (requester, target, capability)."""
    if not _is_human(created_by):
        raise CoveyError(
            403,
            "human_only",
            "Only the human can create grants; agents cannot grant themselves anything.",
            "Ask the human to run `covey approve <request_id>` or `covey grant ...`.",
        )
    if scope not in SCOPES:
        raise CoveyError(
            400, "bad_scope", f"Unknown grant scope {scope!r}.", f"Use one of {SCOPES}."
        )
    if scope == "session" and not session_id:
        raise CoveyError(
            409,
            "no_live_session",
            "A session grant needs the target to have a live session, and it has none.",
            "Grant `once` or `always` instead, or wait until the target's session is running.",
        )
    if scope != "session":
        session_id = None
    row = {
        "grant_id": new_ulid(),
        "requester": requester,
        "target": target,
        "capability": capability,
        "scope": scope,
        "session_id": session_id,
        "created_by": created_by,
        "created_at": store.now_iso(),
        "expires_at": expires_at,
    }
    with store.transaction(conn):
        conn.execute(
            "DELETE FROM grants WHERE requester = ? AND target = ? AND capability = ?",
            (requester, target, capability),
        )
        conn.execute(
            "INSERT INTO grants (grant_id, requester, target, capability, scope, session_id,"
            " created_by, created_at, expires_at) VALUES (:grant_id, :requester, :target,"
            " :capability, :scope, :session_id, :created_by, :created_at, :expires_at)",
            row,
        )
        store.audit(conn, created_by, "grant.created", row["grant_id"], row)
    return row


def revoke_grant(conn: sqlite3.Connection, grant_id: str, actor: str) -> dict[str, Any]:
    with store.transaction(conn):
        row = get_grant(conn, grant_id)
        if row is None:
            raise CoveyError(
                404,
                "grant_not_found",
                f"No grant {grant_id}.",
                "List grants with `covey grants`.",
            )
        conn.execute("DELETE FROM grants WHERE grant_id = ?", (grant_id,))
        store.audit(conn, actor, "grant.revoked", grant_id, row)
    return row


def consume(conn: sqlite3.Connection, grant: Mapping[str, Any], actor: str) -> None:
    """Use up a grant after a successful reply: a `once` grant is deleted, others stay."""
    if grant["scope"] != "once":
        return
    with store.transaction(conn):
        conn.execute("DELETE FROM grants WHERE grant_id = ?", (grant["grant_id"],))
        store.audit(conn, actor, "grant.consumed", grant["grant_id"], dict(grant))
