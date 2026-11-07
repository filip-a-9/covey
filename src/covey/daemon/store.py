"""SQLite schema for coveyd.

This module is part of the contract (see docs/SPEC.md, "Data model & wire protocol").
Change the DDL deliberately, in its own commit, and bump SCHEMA_VERSION with a migration.

Timestamps are ISO-8601 UTC text. Ids are ULIDs except `agents.agent_id`
(`<slug>@<node_id>`) and `audit.id`. `PRAGMA foreign_keys` is per connection, so every
connection the daemon opens must set it again; the line below covers only the one that
creates the schema.
"""

import hashlib
import json
import os
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from covey.config import Config
from covey.ids import new_ulid

SCHEMA_VERSION = 2

_GRANTS_DDL = """
CREATE TABLE grants (
    grant_id    TEXT PRIMARY KEY,
    requester   TEXT NOT NULL REFERENCES agents (agent_id),
    target      TEXT NOT NULL REFERENCES agents (agent_id),
    capability  TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('once', 'session', 'always', 'deny')),
    session_id  TEXT,
    created_by  TEXT NOT NULL REFERENCES agents (agent_id),
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    request_id  TEXT REFERENCES requests (request_id),
    CHECK ((scope = 'session') = (session_id IS NOT NULL))
);
"""
_GRANTS_UNBOUND_INDEX = """
CREATE UNIQUE INDEX grants_unbound ON grants (requester, target, capability)
    WHERE request_id IS NULL;
"""
_GRANTS_REQUEST_INDEX = """
CREATE UNIQUE INDEX grants_request ON grants (request_id) WHERE request_id IS NOT NULL;
"""

DDL = f"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE nodes (
    node_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- The human pseudo-agent (slug 'human') is a row here, so request foreign keys hold.
CREATE TABLE agents (
    agent_id    TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    node_id     TEXT NOT NULL REFERENCES nodes (node_id),
    card        TEXT NOT NULL CHECK (json_valid(card)),
    liveness    TEXT NOT NULL CHECK (liveness IN ('live', 'wakeable', 'gone')),
    last_seen   TEXT NOT NULL,
    session_id  TEXT,
    token_hash  TEXT,
    public_key  TEXT
);

CREATE UNIQUE INDEX agents_slug ON agents (slug);

CREATE TABLE requests (
    request_id       TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    parent_id        TEXT REFERENCES requests (request_id),
    from_agent       TEXT NOT NULL REFERENCES agents (agent_id),
    to_agent         TEXT NOT NULL REFERENCES agents (agent_id),
    capability       TEXT NOT NULL,
    task             TEXT NOT NULL,
    context          TEXT,
    status           TEXT NOT NULL CHECK (status IN (
                         'queued', 'awaiting_grant', 'delivered', 'working',
                         'needs_input', 'done', 'failed', 'refused', 'expired')),
    result           TEXT,
    artifacts        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifacts)),
    hops             INTEGER NOT NULL DEFAULT 0 CHECK (hops BETWEEN 0 AND 4),
    stop_blocked_at  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deadline         TEXT
);

CREATE INDEX requests_inbox ON requests (to_agent, status);
CREATE INDEX requests_thread ON requests (thread_id);

-- capability may be '*' only when the human broadened a grant on purpose.
-- session_id is set exactly when scope is 'session'.
-- request_id is set on a `once` grant the human gave by approving that request: it applies
-- to that request only and goes when the request finishes. Other grants leave it NULL.
{_GRANTS_DDL}

{_GRANTS_UNBOUND_INDEX}
{_GRANTS_REQUEST_INDEX}

-- Append-only. actor is not a foreign key so the log never blocks a write elsewhere.
CREATE TABLE audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    event    TEXT NOT NULL,
    subject  TEXT,
    detail   TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(detail))
);

CREATE INDEX audit_ts ON audit (ts);

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

PRAGMA user_version = {SCHEMA_VERSION};
"""


class SchemaTooNew(RuntimeError):
    pass


@dataclass(frozen=True)
class Node:
    node_id: str
    name: str


def now_iso() -> str:
    """Current UTC time as ISO-8601 with milliseconds and a Z, so text order is time order."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    """Open the database. Autocommit mode: writes go through `transaction()`."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on error. Nested use joins the outer one."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"The database has schema version {version}; this covey knows {SCHEMA_VERSION}. "
            "Upgrade covey (uv tool upgrade covey) or point COVEY_HOME at another directory."
        )
    if version == 0:
        conn.executescript(DDL)
        return
    if version == 1:
        _migrate_1_to_2(conn)
        return
    raise RuntimeError(
        f"No migration from schema version {version} to {SCHEMA_VERSION}."
    )


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Add `grants.request_id` and replace the table's UNIQUE constraint with partial indexes
    (SQLite cannot drop a constraint, so the table is rebuilt). Existing grants keep NULL."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE grants RENAME TO grants_v1")
        conn.execute(_GRANTS_DDL)
        conn.execute(
            "INSERT INTO grants (grant_id, requester, target, capability, scope, session_id,"
            " created_by, created_at, expires_at) SELECT grant_id, requester, target,"
            " capability, scope, session_id, created_by, created_at, expires_at"
            " FROM grants_v1"
        )
        conn.execute("DROP TABLE grants_v1")
        conn.execute(_GRANTS_UNBOUND_INDEX)
        conn.execute(_GRANTS_REQUEST_INDEX)
        conn.execute("PRAGMA user_version = 2")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def audit(
    conn: sqlite3.Connection,
    actor: str,
    event: str,
    subject: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one audit row and return its id."""
    cur = conn.execute(
        "INSERT INTO audit (ts, actor, event, subject, detail) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), actor, event, subject, json.dumps(detail or {})),
    )
    return cur.lastrowid


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


RESERVED_SLUGS = ("human", "coveyd")


def daemon_actor(node_id: str) -> str:
    """The audit actor for changes the daemon makes on its own (sweeps, expiry)."""
    return f"coveyd@{node_id}"


def human_card(node_id: str, last_seen: str) -> dict[str, Any]:
    return {
        "covey": "1",
        "agent_id": f"human@{node_id}",
        "slug": "human",
        "kind": "human",
        "summary": "The person at this machine. Ask when only they can decide or answer.",
        "tags": [],
        "capabilities": [],
        "delivery": "poll",
        "wakeable": False,
        "liveness": "live",
        "last_seen": last_seen,
        "public_key": None,
    }


def init_home(cfg: Config) -> Node:
    """First-run setup, idempotent: home dir, token files, node.json, schema, node and human rows."""
    cfg.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cfg.home, 0o700)
    for path in (cfg.token_path, cfg.join_token_path):
        if not path.exists():
            _write_private(path, secrets.token_urlsafe(32) + "\n")
    if not cfg.node_path.exists():
        node_info = {"node_id": new_ulid(), "name": socket.gethostname()}
        _write_private(cfg.node_path, json.dumps(node_info) + "\n")
    node_info = json.loads(cfg.node_path.read_text())
    node = Node(node_id=node_info["node_id"], name=node_info["name"])

    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        now = now_iso()
        with transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO nodes (node_id, name, created_at) VALUES (?, ?, ?)",
                (node.node_id, node.name, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO agents (agent_id, slug, node_id, card, liveness, last_seen)"
                " VALUES (?, 'human', ?, ?, 'live', ?)",
                (
                    f"human@{node.node_id}",
                    node.node_id,
                    json.dumps(human_card(node.node_id, now)),
                    now,
                ),
            )
    finally:
        conn.close()
    return node
