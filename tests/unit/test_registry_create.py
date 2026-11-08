"""create_agent (covey agent add) and what register keeps on a takeover."""

import json

import pytest

from covey.daemon import peer, registry, store
from covey.daemon.errors import CoveyError


def create(conn, node, slug="docs", **kw):
    body = {"slug": slug, "workdir": "/srv/docs", **kw}
    with store.transaction(conn):
        return registry.create_agent(conn, node.node_id, body)


# A session started in the agent's workdir, outside every other agent's tree.
IN_WORKDIR = peer.Origin(frozenset(), "/srv/docs")


def register(conn, node, slug="docs", origin=IN_WORKDIR, **kw):
    body = {"slug": slug, "kind": "claude-code", "delivery": "hooks", **kw}
    with store.transaction(conn):
        return registry.register(conn, node.node_id, body, origin)


def refusal(fn, *args, **kw):
    with pytest.raises(CoveyError) as exc:
        fn(*args, **kw)
    return exc.value


# ------------------------------------------------------------ create_agent


def test_create_wakeable_agent_has_no_token_and_is_wakeable(conn, node):
    card = create(conn, node, wakeable=True)
    row = registry.get_agent(conn, "docs")
    assert row["token_hash"] is None
    assert row["liveness"] == "wakeable"
    assert card["liveness"] == "wakeable"
    assert card["wakeable"] is True
    assert card["workdir"] == "/srv/docs"
    assert card["agent_id"] == f"docs@{node.node_id}"


def test_create_without_wakeable_is_gone(conn, node):
    card = create(conn, node)
    assert registry.get_agent(conn, "docs")["liveness"] == "gone"
    assert card["wakeable"] is False


def test_create_stores_the_worker_block(conn, node):
    worker = {"allowed_tools": ["Read", "Edit"], "max_turns": 5, "daily_spawns": 3}
    create(conn, node, wakeable=True, worker=worker)
    stored = json.loads(registry.get_agent(conn, "docs")["card"])
    assert stored["worker"] == worker


def test_create_accepts_bash_in_the_worker_tools(conn, node):
    worker = {"allowed_tools": ["Read", "Bash", "Bash(git status:*)"]}
    create(conn, node, wakeable=True, worker=worker)
    stored = json.loads(registry.get_agent(conn, "docs")["card"])
    assert stored["worker"]["allowed_tools"] == ["Read", "Bash", "Bash(git status:*)"]


def test_create_refuses_a_live_slug(conn, node):
    register(conn, node)
    err = refusal(create, conn, node, wakeable=True)
    assert (err.status, err.code) == (409, "slug_live")


def test_create_refuses_reserved_slugs(conn, node):
    err = refusal(create, conn, node, slug="human")
    assert (err.status, err.code) == (409, "slug_reserved")


def test_create_rejects_an_invalid_card(conn, node):
    err = refusal(create, conn, node, worker={"max_turns": 0})
    assert (err.status, err.code) == (400, "invalid_card")


def test_create_updates_a_gone_agent_in_place_keeping_its_token(conn, node):
    _, token = register(conn, node, session_id="s1")
    with store.transaction(conn):
        conn.execute("UPDATE agents SET liveness = 'gone' WHERE slug = 'docs'")
    card = create(conn, node, wakeable=True, summary="Docs repo.")
    rows = conn.execute("SELECT * FROM agents WHERE slug = 'docs'").fetchall()
    assert len(rows) == 1
    assert rows[0]["liveness"] == "wakeable"
    assert rows[0]["token_hash"] == store.hash_token(token)
    assert card["summary"] == "Docs repo."


def test_create_is_audited_as_agent_created(conn, node):
    create(conn, node, wakeable=True)
    rows = conn.execute(
        "SELECT actor, subject FROM audit WHERE event = 'agent.created'"
    ).fetchall()
    assert [(r["actor"], r["subject"]) for r in rows] == [
        (f"human@{node.node_id}", f"docs@{node.node_id}")
    ]


# ------------------------------------------------------- register takeover


def test_register_takeover_keeps_wakeable_when_omitted(conn, node):
    create(conn, node, wakeable=True)
    card, _ = register(conn, node)
    assert card["wakeable"] is True


def test_register_takeover_can_turn_wakeable_off(conn, node):
    create(conn, node, wakeable=True)
    card, _ = register(conn, node, wakeable=False)
    assert card["wakeable"] is False


def test_register_takeover_keeps_the_worker_block(conn, node):
    create(conn, node, wakeable=True, worker={"max_turns": 3})
    card, _ = register(conn, node)
    assert card["worker"] == {"max_turns": 3}


def test_register_cannot_set_a_worker_block(conn, node):
    create(conn, node, wakeable=True, worker={"max_turns": 3})
    card, _ = register(conn, node, worker={"max_turns": 200})
    assert card["worker"] == {"max_turns": 3}
    with store.transaction(conn):
        conn.execute("DELETE FROM agents WHERE slug = 'docs'")
    card, _ = register(conn, node, worker={"max_turns": 200})
    assert "worker" not in card


def test_a_swept_takeover_falls_back_to_wakeable(conn, node):
    """After the shim's session times out, a pre-registered agent is wakeable again."""
    create(conn, node, wakeable=True)
    register(conn, node)
    later = "2999-01-01T00:00:00Z"
    with store.transaction(conn):
        registry.sweep(conn, later, 60)
    assert registry.get_agent(conn, "docs")["liveness"] == "wakeable"


# ------------------------------------------------- takeover of a human-created slug


@pytest.mark.parametrize(
    "origin",
    [
        None,
        peer.Origin(frozenset(), None),
        peer.Origin(frozenset(), "/srv/elsewhere"),
        peer.Origin(frozenset(), "/srv/docs-evil"),
        peer.Origin(frozenset({"other@%s"}), "/srv/docs"),
    ],
    ids=["unknown-peer", "unreadable-cwd", "wrong-cwd", "sibling-dir", "other-tree"],
)
def test_a_takeover_of_a_human_created_slug_needs_the_right_origin(conn, node, origin):
    create(conn, node, wakeable=True)
    if origin is not None and origin.agents:
        origin = peer.Origin(frozenset({f"other@{node.node_id}"}), origin.cwd)
    err = refusal(register, conn, node, origin=origin)
    assert (err.status, err.code) == (409, "slug_owned")


def test_a_takeover_from_a_subdirectory_or_the_slugs_own_old_tree_is_allowed(
    conn, node
):
    create(conn, node, wakeable=True)
    own = peer.Origin(frozenset({f"docs@{node.node_id}"}), "/srv/docs/pkg")
    card, _ = register(conn, node, origin=own)
    assert card["workdir"] == "/srv/docs"
