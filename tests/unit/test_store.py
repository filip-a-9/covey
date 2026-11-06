import json
import sqlite3
import stat

import pytest

from covey.daemon import store


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_first_run_creates_private_home_and_token_files(cfg, node):
    assert mode(cfg.home) == 0o700
    for path in (cfg.token_path, cfg.join_token_path, cfg.node_path):
        assert mode(path) == 0o600
    assert cfg.token_path.read_text().strip() != cfg.join_token_path.read_text().strip()


def test_second_run_keeps_node_id_and_tokens(cfg, node):
    token = cfg.token_path.read_text()
    again = store.init_home(cfg)
    assert again.node_id == node.node_id
    assert cfg.token_path.read_text() == token


def test_first_run_creates_node_and_human_rows(conn, node):
    row = conn.execute("SELECT * FROM agents WHERE slug = 'human'").fetchone()
    assert row["agent_id"] == f"human@{node.node_id}"
    assert json.loads(row["card"])["kind"] == "human"
    assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1


def test_connection_enforces_foreign_keys(conn):
    with pytest.raises(sqlite3.IntegrityError), store.transaction(conn):
        conn.execute(
            "INSERT INTO grants (grant_id, requester, target, capability, scope, created_by,"
            " created_at) VALUES ('g', 'nobody@x', 'nobody@x', 'c', 'always', 'nobody@x', 't')"
        )


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), store.transaction(conn):
        store.audit(conn, "human@x", "test.event")
        raise RuntimeError("boom")
    assert (
        conn.execute(
            "SELECT count(*) FROM audit WHERE event = 'test.event'"
        ).fetchone()[0]
        == 0
    )


def test_audit_returns_increasing_ids(conn):
    with store.transaction(conn):
        first = store.audit(conn, "human@x", "a", subject="s", detail={"k": 1})
        second = store.audit(conn, "human@x", "b")
    assert second > first
    row = conn.execute("SELECT * FROM audit WHERE id = ?", (first,)).fetchone()
    assert json.loads(row["detail"]) == {"k": 1}


def test_migrate_refuses_a_newer_database(tmp_path):
    c = store.connect(tmp_path / "x.db")
    c.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    with pytest.raises(store.SchemaTooNew):
        store.migrate(c)


def test_now_iso_is_utc_with_z(conn):
    assert store.now_iso().endswith("Z")
