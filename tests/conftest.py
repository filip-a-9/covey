"""Shared fixtures: a real Covey home and database in a tmp dir. No mocks."""

import json
import sqlite3

import pytest

from covey.config import Config
from covey.daemon import store


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    """Daemons started from the environment (`covey up` in a subprocess) cannot reach the
    installed `claude` either: a spawned worker would cost money."""
    monkeypatch.setenv("COVEY_CLAUDE", "/nonexistent/claude-is-disabled-in-tests")


@pytest.fixture(autouse=True)
def no_real_toast(monkeypatch):
    """No test may pop a desktop notification, from this process or a daemon subprocess."""
    monkeypatch.setenv("COVEY_NOTIFY", "log")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home",
        port=7777,
        claude_bin="/nonexistent/claude-is-disabled-in-tests",  # never the real one
    )


@pytest.fixture
def node(cfg) -> store.Node:
    return store.init_home(cfg)


@pytest.fixture
def conn(cfg, node) -> sqlite3.Connection:
    c = store.connect(cfg.db_path)
    yield c
    c.close()


def add_agent(
    conn, node, slug, capabilities=(), liveness="live", session_id=None, wakeable=False
):
    """Insert an agent row directly, for tests of modules that only read agents."""
    agent_id = f"{slug}@{node.node_id}"
    card = {
        "covey": "1",
        "agent_id": agent_id,
        "slug": slug,
        "kind": "python",
        "summary": f"test agent {slug}",
        "tags": [],
        "capabilities": [{"name": n, "level": lvl} for n, lvl in capabilities],
        "delivery": "poll",
        "wakeable": wakeable,
        "liveness": liveness,
        "last_seen": store.now_iso(),
        "public_key": None,
    }
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO agents (agent_id, slug, node_id, card, liveness, last_seen, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                slug,
                node.node_id,
                json.dumps(card),
                liveness,
                card["last_seen"],
                session_id,
            ),
        )
    return agent_id
