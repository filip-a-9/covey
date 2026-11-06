import pytest

from covey.daemon import grants, store
from covey.daemon.errors import CoveyError
from tests.conftest import add_agent

NOW = "2026-11-06T12:00:00.000Z"
HUMAN = "human@N"
OTHER = "web@N"


def g(
    capability="run_tests",
    scope="always",
    session_id=None,
    created_by=HUMAN,
    expires_at=None,
):
    return {
        "grant_id": f"{capability}-{scope}",
        "capability": capability,
        "scope": scope,
        "session_id": session_id,
        "created_by": created_by,
        "expires_at": expires_at,
    }


@pytest.mark.parametrize(
    ("level", "rows", "session", "want"),
    [
        ("open", [], None, "allow"),
        ("forbidden", [g(scope="always")], None, "deny"),
        ("forbidden", [g(capability="*")], None, "deny"),
        ("gated", [], None, "ask"),
        (None, [], None, "ask"),
        (None, [g(scope="always")], None, "allow"),
        ("gated", [g(scope="always")], None, "allow"),
        ("gated", [g(scope="once")], None, "allow"),
        ("gated", [g(scope="deny")], None, "deny"),
        ("gated", [g(scope="session", session_id="s1")], "s1", "allow"),
        ("gated", [g(scope="session", session_id="s1")], "s2", "ask"),
        ("gated", [g(scope="session", session_id="s1")], None, "ask"),
        ("gated", [g(capability="*")], None, "allow"),
        ("gated", [g(capability="*", created_by=OTHER)], None, "ask"),
        ("gated", [g(capability="*"), g(scope="deny")], None, "deny"),
        ("gated", [g(scope="deny", capability="*"), g(scope="always")], None, "allow"),
        ("gated", [g(expires_at="2026-11-06T11:59:59.000Z")], None, "ask"),
        ("gated", [g(expires_at="2026-11-06T12:00:01.000Z")], None, "allow"),
        ("gated", [g(capability="edit")], None, "ask"),
    ],
)
def test_decide(level, rows, session, want):
    assert grants.decide(level, "run_tests", rows, session, NOW).outcome == want


def test_decide_names_the_grant_it_used():
    once = g(scope="once")
    assert (
        grants.decide("gated", "run_tests", [g(capability="*"), once], None, NOW).grant
        is once
    )


def test_open_needs_no_grant():
    assert grants.decide("open", "explain", [], None, NOW).grant is None


@pytest.fixture
def pair(conn, node):
    web = add_agent(conn, node, "web")
    api = add_agent(conn, node, "api", session_id="sess-1")
    return web, api, f"human@{node.node_id}"


def test_create_grant_replaces_the_previous_one(conn, pair):
    web, api, human = pair
    first = grants.create_grant(conn, web, api, "run_tests", "once", created_by=human)
    second = grants.create_grant(
        conn, web, api, "run_tests", "always", created_by=human
    )
    rows = grants.list_grants(conn)
    assert [r["grant_id"] for r in rows] == [second["grant_id"]]
    assert second["grant_id"] != first["grant_id"]
    assert rows[0]["scope"] == "always"


def test_agents_cannot_create_grants(conn, pair):
    web, api, _ = pair
    with pytest.raises(CoveyError) as exc:
        grants.create_grant(conn, web, api, "run_tests", "always", created_by=web)
    assert exc.value.status == 403
    assert grants.list_grants(conn) == []


def test_session_grant_needs_a_session(conn, pair):
    web, api, human = pair
    with pytest.raises(CoveyError) as exc:
        grants.create_grant(conn, web, api, "run_tests", "session", created_by=human)
    assert exc.value.status == 409


def test_session_grant_records_the_session(conn, pair):
    web, api, human = pair
    row = grants.create_grant(
        conn, web, api, "run_tests", "session", created_by=human, session_id="sess-1"
    )
    assert row["session_id"] == "sess-1"


def test_matching_grants_returns_exact_and_wildcard_only(conn, pair):
    web, api, human = pair
    grants.create_grant(conn, web, api, "run_tests", "always", created_by=human)
    grants.create_grant(conn, web, api, "*", "always", created_by=human)
    grants.create_grant(conn, web, api, "edit", "always", created_by=human)
    grants.create_grant(conn, api, web, "run_tests", "always", created_by=human)
    caps = sorted(
        r["capability"] for r in grants.matching_grants(conn, web, api, "run_tests")
    )
    assert caps == ["*", "run_tests"]


def test_revoke_removes_the_grant_and_audits(conn, pair):
    web, api, human = pair
    row = grants.create_grant(conn, web, api, "run_tests", "always", created_by=human)
    revoked = grants.revoke_grant(conn, row["grant_id"], actor=human)
    assert revoked["grant_id"] == row["grant_id"]
    assert grants.list_grants(conn) == []
    events = [r["event"] for r in conn.execute("SELECT event FROM audit ORDER BY id")]
    assert events[-2:] == ["grant.created", "grant.revoked"]


def test_revoke_unknown_grant_is_404(conn, pair):
    with pytest.raises(CoveyError) as exc:
        grants.revoke_grant(conn, "01JC0000000000000000000000", actor=pair[2])
    assert exc.value.status == 404


def test_consume_deletes_a_once_grant(conn, pair):
    web, api, human = pair
    row = grants.create_grant(conn, web, api, "run_tests", "once", created_by=human)
    grants.consume(conn, row, actor=api)
    assert grants.list_grants(conn) == []


def test_consume_keeps_an_always_grant(conn, pair):
    web, api, human = pair
    row = grants.create_grant(conn, web, api, "run_tests", "always", created_by=human)
    grants.consume(conn, row, actor=api)
    assert len(grants.list_grants(conn)) == 1


def test_list_grants_involving_an_agent(conn, pair, node):
    web, api, human = pair
    other = add_agent(conn, node, "ops")
    grants.create_grant(conn, web, api, "run_tests", "always", created_by=human)
    grants.create_grant(conn, other, api, "run_tests", "always", created_by=human)
    assert len(grants.list_grants(conn, involving=web)) == 1
    assert len(grants.list_grants(conn, target=api)) == 2


def test_grant_rows_are_api_shaped(conn, pair):
    web, api, human = pair
    row = grants.create_grant(conn, web, api, "run_tests", "always", created_by=human)
    assert set(row) == {
        "grant_id",
        "requester",
        "target",
        "capability",
        "scope",
        "session_id",
        "created_by",
        "created_at",
        "expires_at",
    }
    assert store.now_iso() >= row["created_at"]
