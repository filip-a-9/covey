"""card_of / get_agent / agent_by_token_hash / whoami (written before the build phase)."""

import pytest

from covey.daemon import registry, store
from covey.daemon.errors import CoveyError
from tests.conftest import add_agent


def test_card_of_takes_liveness_from_the_columns(conn, node):
    add_agent(conn, node, "api")
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET liveness = 'gone', last_seen = 'T' WHERE slug = 'api'"
        )
    card = registry.card_of(registry.get_agent(conn, "api"))
    assert (card["liveness"], card["last_seen"]) == ("gone", "T")


def test_get_agent_unknown_slug_is_404(conn):
    with pytest.raises(CoveyError) as exc:
        registry.get_agent(conn, "nope")
    assert exc.value.status == 404


def test_agent_by_token_hash(conn, node):
    add_agent(conn, node, "api")
    with store.transaction(conn):
        conn.execute("UPDATE agents SET token_hash = 'h1' WHERE slug = 'api'")
    assert registry.agent_by_token_hash(conn, "h1")["slug"] == "api"
    assert registry.agent_by_token_hash(conn, "h2") is None


def test_whoami_for_the_human(conn, node):
    me = registry.whoami(conn, f"human@{node.node_id}", node.node_id)
    assert me["card"]["slug"] == "human"
    assert me["node_id"] == node.node_id
    assert me["liveness"] == "live"
