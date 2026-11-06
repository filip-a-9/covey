"""Shared fixtures: a real Covey home and database in a tmp dir. No mocks."""

import json
import sqlite3

import pytest

from covey.config import Config
from covey.daemon import store


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(home=tmp_path / "home", port=7777)


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
