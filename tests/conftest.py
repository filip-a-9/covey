"""Shared fixtures: a real Covey home and database in a tmp dir. No mocks."""

import json
import sqlite3
import sys

import pytest

from covey.config import Config
from covey.daemon import store
from tests import leaks


@pytest.fixture(scope="session", autouse=True)
def no_leaked_demo_daemon():
    """Fail the run if a demo coveyd started during it is still alive at the end
    (.claude/specs/test-daemon-leak.md). Demo daemons already running are left alone."""
    if not sys.platform.startswith("linux"):
        yield
        return
    before = set(leaks.demo_daemons())
    yield
    leaked = {p: e for p, e in leaks.demo_daemons().items() if p not in before}
    if not leaked:
        return
    reports = []
    for pid, env in leaked.items():
        reports.append(leaks.describe(pid, env))
        leaks.stop(pid)
    pytest.fail(
        f"{len(leaked)} demo coveyd outlived the test run and was stopped:\n\n"
        + "\n\n".join(reports),
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    """Daemons started from the environment (`covey up` in a subprocess) cannot reach the
    installed `claude` either: a spawned worker would cost money."""
    monkeypatch.setenv("COVEY_CLAUDE", "/nonexistent/claude-is-disabled-in-tests")


@pytest.fixture(autouse=True)
def no_real_session_socket(monkeypatch):
    """The suite may run inside a Claude Code session. No test may learn that session's
    messaging socket, or a test daemon would post wake messages into it."""
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)


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
