"""register / heartbeat / describe / sweep / find."""

import json
from pathlib import Path

import pytest

from covey.daemon import registry, store
from covey.daemon.errors import CoveyError

RANKING = Path(__file__).parent.parent / "fixtures" / "ranking"


def body(slug="api", **kw):
    return {"slug": slug, "kind": "python", "delivery": "poll", **kw}


def reg(conn, node, slug="api", **kw):
    with store.transaction(conn):
        return registry.register(conn, node.node_id, body(slug, **kw))


def refusal(fn, *args):
    with pytest.raises(CoveyError) as exc:
        fn(*args)
    return exc.value


# ---------------------------------------------------------------- register


def test_register_builds_the_full_card_with_defaults(conn, node):
    card, token = reg(conn, node)
    assert card["covey"] == "1"
    assert card["agent_id"] == f"api@{node.node_id}"
    assert (card["summary"], card["tags"], card["capabilities"]) == ("", [], [])
    assert (card["wakeable"], card["liveness"], card["public_key"]) == (
        False,
        "live",
        None,
    )
    assert "workdir" not in card and "instructions" not in card
    assert token


def test_register_keeps_optional_fields_when_given(conn, node):
    card, _ = reg(
        conn,
        node,
        workdir="/srv/api",
        instructions="Ask nicely.",
        summary="Orders.",
        wakeable=True,
        tags=["a"],
        capabilities=[{"name": "explain", "level": "open"}],
    )
    assert card["workdir"] == "/srv/api"
    assert card["instructions"] == "Ask nicely."
    assert card["wakeable"] is True
    assert card["capabilities"] == [{"name": "explain", "level": "open"}]


def test_register_stores_token_hash_and_session_id_not_the_token(conn, node):
    _, token = reg(conn, node, session_id="s1")
    row = registry.get_agent(conn, "api")
    assert row["token_hash"] == store.hash_token(token)
    assert row["session_id"] == "s1"
    assert token not in row["card"] and "s1" not in row["card"]


def test_register_audits_without_the_token(conn, node):
    _, token = reg(conn, node)
    rows = conn.execute(
        "SELECT event, detail FROM audit WHERE event = 'agent.registered'"
    ).fetchall()
    assert len(rows) == 1
    assert token not in rows[0]["detail"]


def test_register_human_slug_is_reserved(conn, node):
    err = refusal(registry.register, conn, node.node_id, body("human"))
    assert (err.status, err.code) == (409, "slug_reserved")


def test_register_invalid_card_is_400(conn, node):
    err = refusal(
        registry.register,
        conn,
        node.node_id,
        body(capabilities=[{"name": "x", "level": "bogus"}]),
    )
    assert (err.status, err.code) == (400, "invalid_card")


def test_register_webhook_without_url_is_invalid(conn, node):
    b = body()
    b["delivery"] = "webhook"
    err = refusal(registry.register, conn, node.node_id, b)
    assert (err.status, err.code) == (400, "invalid_card")


def test_register_live_slug_is_refused(conn, node):
    reg(conn, node)
    err = refusal(registry.register, conn, node.node_id, body())
    assert (err.status, err.code) == (409, "slug_live")


def test_register_takes_over_a_gone_slug_in_place_and_rotates_token(conn, node):
    _, old = reg(conn, node, session_id="s1")
    with store.transaction(conn):
        conn.execute("UPDATE agents SET liveness = 'gone' WHERE slug = 'api'")
    card, new = reg(conn, node, summary="Second life.", session_id="s2")
    assert new != old
    rows = conn.execute("SELECT * FROM agents WHERE slug = 'api'").fetchall()
    assert len(rows) == 1
    assert (rows[0]["liveness"], rows[0]["session_id"]) == ("live", "s2")
    assert rows[0]["token_hash"] == store.hash_token(new)
    assert card["summary"] == "Second life."


def _go_gone(conn):
    with store.transaction(conn):
        conn.execute("UPDATE agents SET liveness = 'gone' WHERE slug = 'api'")


def test_takeover_keeps_the_description_the_body_omits(conn, node):
    caps = [{"name": "run_tests", "level": "gated", "note": "slow"}]
    reg(
        conn,
        node,
        summary="Owns orders.",
        tags=["orders"],
        capabilities=caps,
        instructions="Ask in plain words.",
    )
    _go_gone(conn)
    card, _ = reg(conn, node)
    assert card["summary"] == "Owns orders."
    assert card["tags"] == ["orders"]
    assert card["capabilities"] == caps
    assert card["instructions"] == "Ask in plain words."


def test_takeover_body_overrides_only_the_fields_it_sends(conn, node):
    caps = [{"name": "explain", "level": "open"}]
    reg(conn, node, summary="Old.", tags=["orders"], capabilities=caps)
    _go_gone(conn)
    card, _ = reg(conn, node, summary="New.")
    assert card["summary"] == "New."
    assert card["tags"] == ["orders"]
    assert card["capabilities"] == caps


def test_first_registration_has_an_empty_description(conn, node):
    card, _ = reg(conn, node)
    assert (card["summary"], card["tags"], card["capabilities"]) == ("", [], [])
    assert "instructions" not in card


def test_takeover_keeps_foreign_keys_valid(conn, node):
    reg(conn, node, "caller")
    reg(conn, node, "api")
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO requests (request_id, thread_id, from_agent, to_agent, capability,"
            " task, status, created_at, updated_at) VALUES ('r1','t1',?,?,'explain','x',"
            "'queued','T','T')",
            (f"caller@{node.node_id}", f"api@{node.node_id}"),
        )
        conn.execute("UPDATE agents SET liveness = 'wakeable' WHERE slug = 'api'")
    reg(conn, node, "api")
    assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


# --------------------------------------------------------------- heartbeat


def test_heartbeat_revives_and_stamps(conn, node):
    reg(conn, node)
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET liveness = 'wakeable', last_seen = '2000-01-01T00:00:00.000Z'"
        )
    out = registry.heartbeat(conn, "api")
    row = registry.get_agent(conn, "api")
    assert out == {"liveness": "live", "last_seen": row["last_seen"]}
    assert row["liveness"] == "live"
    assert row["last_seen"] > "2020"


def test_heartbeat_unknown_slug_is_404(conn):
    assert refusal(registry.heartbeat, conn, "nope").status == 404


def test_heartbeat_for_human_is_404(conn, node):
    assert refusal(registry.heartbeat, conn, "human").status == 404


# ---------------------------------------------------------------- describe


def test_describe_updates_only_descriptive_fields(conn, node):
    reg(conn, node, summary="old", tags=["a"])
    with store.transaction(conn):
        card = registry.describe(
            conn,
            "api",
            {"summary": "new", "tags": ["b", "c"], "kind": "evil", "slug": "x"},
        )
    assert (card["summary"], card["tags"]) == ("new", ["b", "c"])
    assert (card["kind"], card["slug"]) == ("python", "api")
    assert registry.card_of(registry.get_agent(conn, "api"))["summary"] == "new"
    assert (
        conn.execute(
            "SELECT count(*) FROM audit WHERE event = 'agent.described'"
        ).fetchone()[0]
        == 1
    )


def test_describe_invalid_is_400_and_changes_nothing(conn, node):
    reg(conn, node, summary="old")
    err = refusal(registry.describe, conn, "api", {"summary": "x" * 1001})
    assert (err.status, err.code) == (400, "invalid_card")
    assert registry.card_of(registry.get_agent(conn, "api"))["summary"] == "old"


def test_describe_unknown_slug_is_404(conn):
    assert refusal(registry.describe, conn, "nope", {"summary": "x"}).status == 404


# ------------------------------------------------------------------- sweep

OLD = "2026-01-01T00:00:00.000Z"
NOW = "2026-01-01T00:02:00.000Z"


def age(conn, slug, last_seen):
    with store.transaction(conn):
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE slug = ?", (last_seen, slug)
        )


def test_sweep_wakeable_card_becomes_wakeable(conn, node):
    reg(conn, node, "w", wakeable=True, session_id="s1")
    age(conn, "w", OLD)
    with store.transaction(conn):
        changed = registry.sweep(conn, NOW, 60)
    row = registry.get_agent(conn, "w")
    assert changed == [f"w@{node.node_id}"]
    assert row["liveness"] == "wakeable"
    assert (
        conn.execute(
            "SELECT count(*) FROM audit WHERE event = 'agent.liveness'"
        ).fetchone()[0]
        == 1
    )


def test_sweep_non_wakeable_becomes_gone(conn, node):
    reg(conn, node, "g")
    age(conn, "g", OLD)
    with store.transaction(conn):
        registry.sweep(conn, NOW, 60)
    assert registry.get_agent(conn, "g")["liveness"] == "gone"


def test_sweep_leaves_fresh_agents_and_the_human(conn, node):
    reg(conn, node, "fresh")
    age(conn, "fresh", "2026-01-01T00:01:30.000Z")
    age(conn, "human", OLD)
    with store.transaction(conn):
        changed = registry.sweep(conn, NOW, 60)
    assert changed == []
    assert registry.get_agent(conn, "fresh")["liveness"] == "live"
    assert registry.get_agent(conn, "human")["liveness"] == "live"


def test_sweep_ignores_agents_already_not_live(conn, node):
    reg(conn, node, "g")
    age(conn, "g", OLD)
    with store.transaction(conn):
        registry.sweep(conn, NOW, 60)
        assert registry.sweep(conn, NOW, 60) == []


# -------------------------------------------------------------------- find


def names(matches):
    return [m["card"]["slug"] for m in matches]


def test_find_without_need_lists_non_gone_agents_by_slug_excluding_human(conn, node):
    reg(conn, node, "zeta")
    reg(conn, node, "alpha")
    reg(conn, node, "dead")
    with store.transaction(conn):
        conn.execute("UPDATE agents SET liveness = 'gone' WHERE slug = 'dead'")
    out = registry.find(conn)
    assert names(out) == ["alpha", "zeta"]
    assert all(m["score"] == 0 for m in out)


def test_find_capability_filter_excludes_forbidden_and_missing(conn, node):
    reg(conn, node, "a", capabilities=[{"name": "run_tests", "level": "open"}])
    reg(conn, node, "b", capabilities=[{"name": "run_tests", "level": "forbidden"}])
    reg(conn, node, "c")
    assert names(registry.find(conn, capability="run_tests")) == ["a"]


def test_find_limit(conn, node):
    for s in "abc":
        reg(conn, node, s)
    assert names(registry.find(conn, limit=2)) == ["a", "b"]


def test_find_drops_zero_relevance_even_when_live(conn, node):
    reg(conn, node, "cooking", summary="Recipes and kitchen timers.")
    assert registry.find(conn, need="terraform modules") == []


def test_find_capability_name_outweighs_tag_and_summary_overlap(conn, node):
    reg(conn, node, "cap", capabilities=[{"name": "run_tests", "level": "open"}])
    reg(conn, node, "tag", tags=["tests"])
    reg(conn, node, "sum", summary="We write tests for everything.")
    out = registry.find(conn, need="run tests")
    assert names(out) == ["cap", "tag", "sum"]


def test_find_scores_use_the_spec_weights(conn, node):
    # One capability token match (3) + live bonus (2); "run_tests" splits to run, tests.
    reg(conn, node, "cap", capabilities=[{"name": "deploy", "level": "open"}])
    out = registry.find(conn, need="deploy")
    assert out[0]["score"] == 5


def test_find_liveness_bonus_breaks_otherwise_equal_cards(conn, node):
    reg(conn, node, "live-one", tags=["billing"])
    reg(conn, node, "wake-one", tags=["billing"], wakeable=True)
    age(conn, "wake-one", OLD)
    with store.transaction(conn):
        registry.sweep(conn, NOW, 60)
        # keep live-one live
        conn.execute("UPDATE agents SET last_seen = ? WHERE slug = 'live-one'", (NOW,))
    assert names(registry.find(conn, need="billing")) == ["live-one", "wake-one"]


def test_find_ties_are_broken_by_slug(conn, node):
    reg(conn, node, "b", tags=["billing"])
    reg(conn, node, "a", tags=["billing"])
    assert names(registry.find(conn, need="billing")) == ["a", "b"]


def test_find_plural_and_case_are_normalised(conn, node):
    reg(conn, node, "x", tags=["Invoice"])
    assert names(registry.find(conn, need="all my INVOICES")) == ["x"]


def test_find_stopwords_alone_match_nothing(conn, node):
    reg(conn, node, "x", summary="The agent that is for the team.")
    assert registry.find(conn, need="the that is for") == []


def test_find_uses_recent_request_topics(conn, node):
    reg(conn, node, "caller")
    reg(conn, node, "quiet", summary="Generic helper.")
    reg(conn, node, "busy", summary="Generic helper.")
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO requests (request_id, thread_id, from_agent, to_agent, capability,"
            " task, status, created_at, updated_at) VALUES ('r1','t1',?,?,'explain',"
            "'why does kubernetes crash','done','T','T')",
            (f"caller@{node.node_id}", f"busy@{node.node_id}"),
        )
    out = registry.find(conn, need="kubernetes problem")
    assert names(out) == ["busy"]
    assert "request" in out[0]["why"]


def test_find_only_looks_at_the_last_20_requests(conn, node):
    reg(conn, node, "caller")
    reg(conn, node, "busy", summary="Generic helper.")
    with store.transaction(conn):
        for i in range(21):
            task = "ancient kubernetes question" if i == 0 else f"filler {i}"
            conn.execute(
                "INSERT INTO requests (request_id, thread_id, from_agent, to_agent,"
                " capability, task, status, created_at, updated_at)"
                " VALUES (?,?,?,?,'explain',?,'done',?,?)",
                (
                    f"r{i}",
                    f"t{i}",
                    f"caller@{node.node_id}",
                    f"busy@{node.node_id}",
                    task,
                    f"2026-01-01T00:00:{i:02d}.000Z",
                    f"2026-01-01T00:00:{i:02d}.000Z",
                ),
            )
    assert registry.find(conn, need="kubernetes") == []


def test_find_why_names_what_matched(conn, node):
    reg(
        conn,
        node,
        "orders",
        summary="Owns the checkout endpoint.",
        tags=["orders", "billing"],
        capabilities=[{"name": "run_tests", "level": "open"}],
    )
    why = registry.find(conn, need="run tests for orders and billing endpoint")[0][
        "why"
    ]
    for word in ("run_tests", "orders", "billing", "endpoint"):
        assert word in why


# ------------------------------------------- ranking fixtures (SPEC question 4)

CARDS = json.loads((RANKING / "cards.json").read_text())
QUERIES = json.loads((RANKING / "queries.json").read_text())
# Queries token overlap genuinely cannot win (no shared token with the right card).
KNOWN_MISSES = {
    "remind me tomorrow morning about the dentist": "remind vs reminders needs stemming",
    "where is the database schema for payments defined?": "payments vs billing is a synonym",
}


@pytest.fixture
def mesh(conn, node):
    with store.transaction(conn):
        for card in CARDS:
            registry.register(conn, node.node_id, card)
    return conn


@pytest.mark.parametrize("q", QUERIES, ids=[q["need"][:50] for q in QUERIES])
def test_ranking_finds_the_right_agent_among_eight(mesh, q):
    if q["need"] in KNOWN_MISSES:
        pytest.xfail(KNOWN_MISSES[q["need"]])
    top = registry.find(mesh, need=q["need"], limit=1)
    assert names(top) == [q["expect"]]


def test_heartbeat_after_a_sweep_keeps_the_session(conn, node):
    """A session that missed one heartbeat window is the same session when it comes back."""
    reg(conn, node, "w", wakeable=True, session_id="s1")
    age(conn, "w", OLD)
    registry.sweep(conn, NOW, 60)
    registry.heartbeat(conn, "w")
    row = registry.get_agent(conn, "w")
    assert (row["liveness"], row["session_id"]) == ("live", "s1")


def test_sweep_is_audited_as_the_daemon(conn, node):
    reg(conn, node, "w")
    age(conn, "w", OLD)
    registry.sweep(conn, NOW, 60)
    actor = conn.execute(
        "SELECT actor FROM audit WHERE event = 'agent.liveness'"
    ).fetchone()[0]
    assert actor == f"coveyd@{node.node_id}"


def test_register_coveyd_slug_is_reserved(conn, node):
    err = refusal(reg, conn, node, "coveyd")
    assert (err.status, err.code) == (409, "slug_reserved")
