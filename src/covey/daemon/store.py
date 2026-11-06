"""SQLite schema for coveyd.

This module is part of the contract (see docs/SPEC.md, "Data model & wire protocol").
Change the DDL deliberately, in its own commit, and bump SCHEMA_VERSION with a migration.

Timestamps are ISO-8601 UTC text. Ids are ULIDs except `agents.agent_id`
(`<slug>@<node_id>`) and `audit.id`. `PRAGMA foreign_keys` is per connection, so every
connection the daemon opens must set it again; the line below covers only the one that
creates the schema.
"""

SCHEMA_VERSION = 1

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
    UNIQUE (requester, target, capability),
    CHECK ((scope = 'session') = (session_id IS NOT NULL))
);

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
