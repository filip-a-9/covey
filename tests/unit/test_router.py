import json

import pytest

from covey.daemon import grants, router, store
from covey.daemon.errors import CoveyError
from tests.conftest import add_agent


@pytest.fixture
def world(conn, node):
    """alice asks bob (open `review`, gated `deploy`, forbidden `wipe`; `mystery` undeclared)."""
    caps = [("review", "open"), ("deploy", "gated"), ("wipe", "forbidden")]
    return {
        "alice": add_agent(conn, node, "alice"),
        "bob": add_agent(conn, node, "bob", caps, session_id="S1"),
        "carol": add_agent(conn, node, "carol", [("review", "open")]),
        "human": f"human@{node.node_id}",
    }


def ask(conn, world, capability="review", to="bob", frm="alice", **kw):
    return router.create_request(conn, world[frm], to, capability, "do it", **kw)


def audit_events(conn, subject=None):
    rows = conn.execute("SELECT event, subject FROM audit ORDER BY id").fetchall()
    return [r["event"] for r in rows if subject is None or r["subject"] == subject]


def stamp(conn, *requests):
    """Give requests distinct, increasing created_at values so 'oldest first' is unambiguous."""
    for i, r in enumerate(requests, start=1):
        conn.execute(
            "UPDATE requests SET created_at = ? WHERE request_id = ?",
            (f"2026-01-01T00:00:0{i}.000Z", r["request_id"]),
        )


def code_of(excinfo):
    return (excinfo.value.status, excinfo.value.code)


# create_request


@pytest.mark.parametrize("capability", ["wipe\n", "wipe\r", "wipe "])
def test_a_forbidden_name_with_trailing_whitespace_is_not_a_new_capability(
    conn, world, capability
):
    """`$` matches before a final newline; `wipe\\n` must not slip past `forbidden`."""
    with pytest.raises(CoveyError) as e:
        ask(conn, world, capability)
    assert e.value.status in (400, 422)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("capability", ["*", "Run Tests", "", "a*"])
def test_capability_must_be_a_capability_name(conn, world, capability):
    """`*` exists only in grants the human makes; an ask can never smuggle one in."""
    with pytest.raises(CoveyError) as e:
        ask(conn, world, capability)
    assert code_of(e) == (400, "bad_capability")
    assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def test_expiry_is_audited_as_the_daemon(conn, world, node):
    r = ask(conn, world, timeout_s=1)
    router.expire_due(conn, "2100-01-01T00:00:00.000Z")
    actor = conn.execute(
        "SELECT actor FROM audit WHERE event = 'request.expired' AND subject = ?",
        (r["request_id"],),
    ).fetchone()[0]
    assert actor == f"coveyd@{node.node_id}"


def test_open_capability_is_queued_in_a_new_thread(conn, world):
    r = ask(conn, world, context="ctx")
    assert r["status"] == "queued"
    assert r["thread_id"] == r["request_id"]
    assert (r["hops"], r["parent_id"], r["deadline"], r["artifacts"]) == (
        0,
        None,
        None,
        [],
    )
    assert (r["from_agent"], r["to_agent"], r["context"]) == (
        world["alice"],
        world["bob"],
        "ctx",
    )
    assert "stop_blocked_at" not in r
    assert audit_events(conn, r["request_id"]) == ["request.created"]


def test_unknown_target_is_404(conn, world):
    with pytest.raises(CoveyError) as e:
        ask(conn, world, to="nobody")
    assert code_of(e) == (404, "agent_not_found")


def test_gone_target_is_unreachable(conn, world, node):
    add_agent(conn, node, "dead", [("review", "open")], liveness="gone")
    with pytest.raises(CoveyError) as e:
        ask(conn, world, to="dead")
    assert code_of(e) == (422, "target_unreachable")


def test_wakeable_target_is_accepted(conn, world, node):
    add_agent(conn, node, "sleepy", [("review", "open")], liveness="wakeable")
    assert ask(conn, world, to="sleepy")["status"] == "queued"


def test_forbidden_capability_is_refused(conn, world):
    r = ask(conn, world, "wipe")
    assert r["status"] == "refused"
    assert r["result"] is not None


def test_gated_capability_waits_and_audits_approval(conn, world):
    r = ask(conn, world, "deploy")
    assert r["status"] == "awaiting_grant"
    assert audit_events(conn, r["request_id"]) == [
        "request.created",
        "approval.requested",
    ]


def test_undeclared_capability_is_gated(conn, world):
    assert ask(conn, world, "mystery")["status"] == "awaiting_grant"


def test_request_to_human_needs_no_grant(conn, world):
    assert ask(conn, world, "anything", to="human")["status"] == "queued"


def test_always_grant_lets_gated_request_through(conn, world):
    grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "always", world["human"]
    )
    assert ask(conn, world, "deploy")["status"] == "queued"


def test_deny_grant_refuses(conn, world):
    grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "deny", world["human"]
    )
    assert ask(conn, world, "deploy")["status"] == "refused"


def test_session_grant_matches_only_live_target_session(conn, world):
    grants.create_grant(
        conn,
        world["alice"],
        world["bob"],
        "deploy",
        "session",
        world["human"],
        session_id="S1",
    )
    assert ask(conn, world, "deploy")["status"] == "queued"
    conn.execute("UPDATE agents SET session_id = 'S2' WHERE slug = 'bob'")
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"
    conn.execute(
        "UPDATE agents SET session_id = 'S1', liveness = 'wakeable' WHERE slug = 'bob'"
    )
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"


def test_timeout_sets_deadline(conn, world):
    r = ask(conn, world, timeout_s=3600)
    assert r["deadline"] > store.now_iso()
    assert r["deadline"] < "9"
    assert r["deadline"] > r["created_at"]


def test_parent_inherits_thread_and_counts_hops(conn, world):
    root = ask(conn, world)
    child = router.create_request(
        conn, world["bob"], "carol", "review", "sub", parent_id=root["request_id"]
    )
    assert child["thread_id"] == root["thread_id"]
    assert (child["hops"], child["parent_id"]) == (1, root["request_id"])


def test_unknown_parent_is_404(conn, world):
    with pytest.raises(CoveyError) as e:
        ask(conn, world, parent_id="01AAAAAAAAAAAAAAAAAAAAAAAA")
    assert code_of(e) == (404, "request_not_found")


def test_parent_from_outsider_is_403(conn, world):
    root = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        router.create_request(
            conn, world["carol"], "bob", "review", "x", parent_id=root["request_id"]
        )
    assert code_of(e) == (403, "not_a_participant")


def test_thread_id_disagreeing_with_parent_is_400(conn, world):
    root = ask(conn, world)
    other = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        ask(conn, world, parent_id=root["request_id"], thread_id=other["thread_id"])
    assert code_of(e) == (400, "thread_mismatch")


def test_matching_thread_id_with_parent_is_fine(conn, world):
    root = ask(conn, world)
    child = ask(conn, world, parent_id=root["request_id"], thread_id=root["thread_id"])
    assert child["hops"] == 1


def test_thread_without_parent_uses_max_hops_plus_one(conn, world):
    root = ask(conn, world)
    c1 = router.create_request(
        conn, world["bob"], "carol", "review", "x", parent_id=root["request_id"]
    )
    assert c1["hops"] == 1
    joined = ask(conn, world, thread_id=root["thread_id"])
    assert (joined["thread_id"], joined["hops"]) == (root["thread_id"], 2)


def test_unknown_thread_is_404(conn, world):
    with pytest.raises(CoveyError) as e:
        ask(conn, world, thread_id="01AAAAAAAAAAAAAAAAAAAAAAAA")
    assert code_of(e) == (404, "thread_not_found")


def test_thread_from_outsider_is_403(conn, world):
    root = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        router.create_request(
            conn, world["carol"], "bob", "review", "x", thread_id=root["thread_id"]
        )
    assert code_of(e) == (403, "not_a_participant")


def test_hops_above_limit_refused_before_insert(conn, world):
    r = ask(conn, world)
    for _ in range(4):
        r = ask(conn, world, parent_id=r["request_id"])
    assert r["hops"] == 4
    before = conn.execute("SELECT count(*) FROM requests").fetchone()[0]
    with pytest.raises(CoveyError) as e:
        ask(conn, world, parent_id=r["request_id"])
    assert code_of(e) == (422, "hops_exceeded")
    assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == before


def test_custom_max_hops(conn, world):
    root = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        ask(conn, world, parent_id=root["request_id"], max_hops=0)
    assert code_of(e) == (422, "hops_exceeded")


# get_request / is_participant


def test_get_request_and_missing(conn, world):
    r = ask(conn, world)
    assert router.get_request(conn, r["request_id"]) == r
    with pytest.raises(CoveyError) as e:
        router.get_request(conn, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    assert code_of(e) == (404, "request_not_found")


def test_is_participant(conn, world):
    r = ask(conn, world)
    assert router.is_participant(r, world["alice"])
    assert router.is_participant(r, world["bob"])
    assert not router.is_participant(r, world["carol"])


# inbox


def test_inbox_delivers_queued_oldest_first(conn, world):
    r1, r2 = ask(conn, world), ask(conn, world)
    ask(conn, world, to="carol")
    stamp(conn, r1, r2)
    got = router.inbox(conn, world["bob"])
    assert [r["request_id"] for r in got] == [r1["request_id"], r2["request_id"]]
    assert {r["status"] for r in got} == {"delivered"}
    assert router.get_request(conn, r1["request_id"])["status"] == "delivered"
    assert audit_events(conn, r1["request_id"]) == [
        "request.created",
        "request.delivered",
    ]
    assert len(router.inbox(conn, world["bob"])) == 2
    assert audit_events(conn, r1["request_id"]).count("request.delivered") == 1


def test_inbox_default_skips_gated_and_final(conn, world):
    ask(conn, world, "deploy")
    done = ask(conn, world)
    router.reply(conn, done["request_id"], world["bob"], "ok", "done")
    assert router.inbox(conn, world["bob"]) == []


def test_inbox_status_filter(conn, world):
    done = ask(conn, world)
    router.reply(conn, done["request_id"], world["bob"], "ok", "done")
    ask(conn, world)
    got = router.inbox(conn, world["bob"], status="done")
    assert [r["request_id"] for r in got] == [done["request_id"]]


def test_inbox_never_shows_requests_awaiting_grant(conn, world):
    ask(conn, world, "deploy")
    with pytest.raises(CoveyError) as e:
        router.inbox(conn, world["bob"], status="awaiting_grant")
    assert code_of(e) == (400, "bad_status")


def test_peeking_at_an_inbox_does_not_deliver(conn, world):
    r = ask(conn, world)
    got = router.inbox(conn, world["bob"], deliver=False)
    assert [x["status"] for x in got] == ["queued"]
    assert router.get_request(conn, r["request_id"])["status"] == "queued"


# reply


def test_reply_done_stores_result_and_artifacts(conn, world):
    r = ask(conn, world)
    art = [{"name": "a.txt", "content": "hi"}]
    out = router.reply(conn, r["request_id"], world["bob"], "all good", "done", art)
    assert (out["status"], out["result"], out["artifacts"]) == ("done", "all good", art)
    assert "request.replied" in audit_events(conn, r["request_id"])


def test_only_target_may_reply(conn, world):
    r = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["alice"], "x", "done")
    assert code_of(e) == (403, "not_target")


def test_reply_unknown_request(conn, world):
    with pytest.raises(CoveyError) as e:
        router.reply(conn, "01AAAAAAAAAAAAAAAAAAAAAAAA", world["bob"], "x", "done")
    assert code_of(e) == (404, "request_not_found")


def test_reply_with_bad_status_is_400(conn, world):
    r = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "x", "queued")
    assert code_of(e) == (400, "bad_status")


def test_reply_while_awaiting_grant_is_409(conn, world):
    r = ask(conn, world, "deploy")
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "x", "done")
    assert code_of(e) == (409, "awaiting_grant")


def test_identical_second_reply_is_idempotent(conn, world):
    r = ask(conn, world)
    first = router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    again = router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert again == first
    assert audit_events(conn, r["request_id"]).count("request.replied") == 1


def test_different_second_reply_is_409(conn, world):
    r = ask(conn, world)
    router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "other", "done")
    assert code_of(e) == (409, "already_finished")


def test_reply_to_refused_request_is_409(conn, world):
    r = ask(conn, world, "wipe")
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert code_of(e) == (409, "already_finished")


def test_needs_input_keeps_request_open_for_a_final_reply(conn, world):
    r = ask(conn, world)
    mid = router.reply(
        conn, r["request_id"], world["bob"], "which branch?", "needs_input"
    )
    assert mid["status"] == "needs_input"
    assert (
        router.reply(conn, r["request_id"], world["bob"], "ok", "failed")["status"]
        == "failed"
    )


def test_once_grant_consumed_on_done(conn, world):
    g = grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "once", world["human"]
    )
    r = ask(conn, world, "deploy")
    router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert grants.get_grant(conn, g["grant_id"]) is None
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"


def test_once_grant_allows_one_request_in_flight(conn, world):
    g = grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "once", world["human"]
    )
    first = ask(conn, world, "deploy")
    assert first["status"] == "queued"
    second = ask(conn, world, "deploy")
    assert second["status"] == "awaiting_grant"
    router.reply(conn, first["request_id"], world["bob"], "ok", "done")
    assert grants.get_grant(conn, g["grant_id"]) is None
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"


def test_once_grant_in_flight_limit_is_per_requester_and_capability(conn, world):
    grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "once", world["human"]
    )
    assert ask(conn, world, "deploy")["status"] == "queued"
    assert ask(conn, world, "review")["status"] == "queued"


def test_wildcard_once_grant_limits_in_flight_across_capabilities(conn, node, world):
    dave = add_agent(
        conn,
        node,
        "dave",
        [("deploy", "gated"), ("publish", "gated")],
        session_id="S2",
    )
    grants.create_grant(conn, world["alice"], dave, "*", "once", world["human"])
    first = router.create_request(conn, world["alice"], "dave", "deploy", "do it")
    second = router.create_request(conn, world["alice"], "dave", "publish", "do it")
    assert first["status"] == "queued"
    assert second["status"] == "awaiting_grant"


def test_always_grant_allows_many_in_flight(conn, world):
    grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "always", world["human"]
    )
    assert ask(conn, world, "deploy")["status"] == "queued"
    assert ask(conn, world, "deploy")["status"] == "queued"


def test_once_grant_consumed_on_failed_but_not_needs_input(conn, world):
    g = grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "once", world["human"]
    )
    r = ask(conn, world, "deploy")
    router.reply(conn, r["request_id"], world["bob"], "q?", "needs_input")
    assert grants.get_grant(conn, g["grant_id"]) is not None
    router.reply(conn, r["request_id"], world["bob"], "boom", "failed")
    assert grants.get_grant(conn, g["grant_id"]) is None


def test_always_grant_survives_reply(conn, world):
    g = grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "always", world["human"]
    )
    r = ask(conn, world, "deploy")
    router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert grants.get_grant(conn, g["grant_id"]) is not None


def test_revoked_grant_fails_request_at_reply_time(conn, world):
    g = grants.create_grant(
        conn, world["alice"], world["bob"], "deploy", "always", world["human"]
    )
    r = ask(conn, world, "deploy")
    grants.revoke_grant(conn, g["grant_id"], world["human"])
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert code_of(e) == (403, "grant_revoked")
    stored = router.get_request(conn, r["request_id"])
    assert (stored["status"], stored["result"]) == ("failed", "grant_revoked")


def test_session_grant_lapses_when_session_changes(conn, world):
    grants.create_grant(
        conn,
        world["alice"],
        world["bob"],
        "deploy",
        "session",
        world["human"],
        session_id="S1",
    )
    r = ask(conn, world, "deploy")
    conn.execute("UPDATE agents SET session_id = 'S2' WHERE slug = 'bob'")
    with pytest.raises(CoveyError) as e:
        router.reply(conn, r["request_id"], world["bob"], "ok", "done")
    assert code_of(e) == (403, "grant_revoked")


def test_reply_to_human_request_needs_no_grant(conn, world):
    r = ask(conn, world, "question", to="human")
    out = router.reply(conn, r["request_id"], world["human"], "yes", "done")
    assert out["status"] == "done"


def test_open_capability_reply_ignores_grants(conn, world):
    r = ask(conn, world)
    assert (
        router.reply(conn, r["request_id"], world["bob"], "ok", "done")["status"]
        == "done"
    )


# approvals


def test_pending_approvals_lists_awaiting_oldest_first(conn, world):
    a, b = ask(conn, world, "deploy"), ask(conn, world, "mystery")
    ask(conn, world)
    stamp(conn, a, b)
    got = router.pending_approvals(conn)
    assert [r["request_id"] for r in got] == [a["request_id"], b["request_id"]]


def test_approve_once_queues_and_creates_grant(conn, world):
    r = ask(conn, world, "deploy")
    req, grant = router.answer_approval(conn, r["request_id"], "once", world["human"])
    assert req["status"] == "queued"
    assert (
        grant["scope"],
        grant["requester"],
        grant["target"],
        grant["capability"],
    ) == (
        "once",
        world["alice"],
        world["bob"],
        "deploy",
    )
    assert grants.get_grant(conn, grant["grant_id"]) is not None
    assert "approval.answered" in audit_events(conn, r["request_id"])


def test_approve_session_ties_grant_to_target_session(conn, world):
    r = ask(conn, world, "deploy")
    _, grant = router.answer_approval(conn, r["request_id"], "session", world["human"])
    assert grant["session_id"] == "S1"


def test_approve_session_without_live_target_is_409_and_changes_nothing(conn, world):
    r = ask(conn, world, "deploy")
    conn.execute("UPDATE agents SET liveness = 'wakeable' WHERE slug = 'bob'")
    with pytest.raises(CoveyError) as e:
        router.answer_approval(conn, r["request_id"], "session", world["human"])
    assert code_of(e) == (409, "no_live_session")
    assert router.get_request(conn, r["request_id"])["status"] == "awaiting_grant"


def test_deny_refuses_request(conn, world):
    r = ask(conn, world, "deploy")
    req, grant = router.answer_approval(conn, r["request_id"], "deny", world["human"])
    assert (req["status"], grant["scope"]) == ("refused", "deny")


def test_answering_a_non_awaiting_request_is_409(conn, world):
    r = ask(conn, world)
    with pytest.raises(CoveyError) as e:
        router.answer_approval(conn, r["request_id"], "once", world["human"])
    assert code_of(e) == (409, "not_awaiting_grant")


def test_agent_cannot_answer_approval(conn, world):
    r = ask(conn, world, "deploy")
    with pytest.raises(CoveyError) as e:
        router.answer_approval(conn, r["request_id"], "always", world["alice"])
    assert code_of(e) == (403, "human_only")
    assert router.get_request(conn, r["request_id"])["status"] == "awaiting_grant"


# expire_due


def test_expire_due_marks_open_requests_with_reasons(conn, world):
    waiting = ask(conn, world, "deploy", timeout_s=10)
    queued = ask(conn, world, timeout_s=10)
    later = ask(conn, world, timeout_s=10000)
    no_deadline = ask(conn, world)
    finished = ask(conn, world, timeout_s=10)
    router.reply(conn, finished["request_id"], world["bob"], "ok", "done")
    future = "2999-01-01T00:00:00.000Z"
    conn.execute(
        "UPDATE requests SET deadline = '2999-01-01T00:00:00.000Z' WHERE request_id = ?",
        (later["request_id"],),
    )
    ids = router.expire_due(conn, "2100-01-01T00:00:00.000Z")
    assert sorted(ids) == sorted([waiting["request_id"], queued["request_id"]])

    def get(r):
        return router.get_request(conn, r["request_id"])

    assert (get(waiting)["status"], get(waiting)["result"]) == (
        "expired",
        "awaiting_grant_timeout",
    )
    assert (get(queued)["status"], get(queued)["result"]) == (
        "expired",
        "deadline_passed",
    )
    assert get(later)["status"] == "queued"
    assert get(no_deadline)["status"] == "queued"
    assert get(finished)["status"] == "done"
    assert "request.expired" in audit_events(conn, waiting["request_id"])
    assert router.expire_due(conn, future) == [later["request_id"]]
    assert router.expire_due(conn, future) == []


# log


def seed_log(conn, world):
    r1 = ask(conn, world)
    r2 = router.create_request(
        conn, world["bob"], "carol", "review", "sub", parent_id=r1["request_id"]
    )
    r3 = ask(conn, world, to="carol")  # unrelated thread: alice -> carol
    stamp(conn, r1, r2, r3)
    return r1, r2, r3


def ids(rows):
    return [r["request_id"] for r in rows]


def test_log_everything_oldest_first_with_decoded_detail(conn, world):
    r1, r2, r3 = seed_log(conn, world)
    out = router.log(conn)
    assert ids(out["requests"]) == [
        r1["request_id"],
        r2["request_id"],
        r3["request_id"],
    ]
    first = out["events"][0]
    assert (first["event"], first["subject"], first["actor"]) == (
        "request.created",
        r1["request_id"],
        world["alice"],
    )
    assert isinstance(first["detail"], dict)
    assert set(first) == {"id", "ts", "actor", "event", "subject", "detail"}
    assert [e["id"] for e in out["events"]] == sorted(e["id"] for e in out["events"])


def test_log_by_thread(conn, world):
    r1, r2, _ = seed_log(conn, world)
    out = router.log(conn, thread=r1["thread_id"])
    assert ids(out["requests"]) == [r1["request_id"], r2["request_id"]]
    assert {e["subject"] for e in out["events"]} == {r1["request_id"], r2["request_id"]}


def test_log_by_agent_slug_and_id(conn, world):
    r1, r2, r3 = seed_log(conn, world)
    by_slug = router.log(conn, agent="bob")
    assert ids(by_slug["requests"]) == [r1["request_id"], r2["request_id"]]
    assert router.log(conn, agent=world["bob"]) == by_slug
    assert {e["subject"] for e in by_slug["events"]} == {
        r1["request_id"],
        r2["request_id"],
    }
    carol = router.log(conn, agent="carol")
    assert ids(carol["requests"]) == [r2["request_id"], r3["request_id"]]


def test_log_agent_events_include_ones_it_acted_in(conn, world):
    r1, _, _ = seed_log(conn, world)
    grants.create_grant(
        conn, world["carol"], world["alice"], "x", "always", world["human"]
    )
    out = router.log(conn, agent="alice")
    assert "grant.created" not in [e["event"] for e in out["events"]]
    router.reply(conn, r1["request_id"], world["bob"], "ok", "done")
    out = router.log(conn, agent="bob")
    assert ("request.replied", world["bob"]) in [
        (e["event"], e["actor"]) for e in out["events"]
    ]


def test_log_unknown_agent_is_404(conn, world):
    with pytest.raises(CoveyError) as e:
        router.log(conn, agent="nobody")
    assert code_of(e) == (404, "agent_not_found")


def test_log_since_and_limit(conn, world):
    r1, r2, r3 = seed_log(conn, world)
    conn.execute(
        "UPDATE requests SET created_at = '2000-01-01T00:00:00.000Z' WHERE request_id = ?",
        (r1["request_id"],),
    )
    out = router.log(conn, since="2001-01-01T00:00:00.000Z")
    assert ids(out["requests"]) == [r2["request_id"], r3["request_id"]]
    out = router.log(conn, limit=1)
    assert len(out["requests"]) == 1 and len(out["events"]) == 1
    assert out["requests"][0]["request_id"] == r3["request_id"]


def test_log_limit_keeps_the_newest_shown_oldest_first(conn, world):
    _, r2, r3 = seed_log(conn, world)
    out = router.log(conn, limit=2)
    assert ids(out["requests"]) == [r2["request_id"], r3["request_id"]]
    all_ids = [e["id"] for e in router.log(conn, limit=500)["events"]]
    assert [e["id"] for e in out["events"]] == all_ids[-2:]


def test_log_visible_to_restricts_to_own_threads(conn, world):
    r1, r2, r3 = seed_log(conn, world)
    grants.create_grant(
        conn, world["carol"], world["alice"], "x", "always", world["human"]
    )
    out = router.log(conn, visible_to=world["bob"])
    assert ids(out["requests"]) == [r1["request_id"], r2["request_id"]]
    assert {e["subject"] for e in out["events"]} == {r1["request_id"], r2["request_id"]}
    assert ids(router.log(conn, visible_to=world["carol"])["requests"]) == [
        r1["request_id"],
        r2["request_id"],
        r3["request_id"],
    ]


def test_log_events_decode_json_detail(conn, world):
    ask(conn, world, "deploy")
    ev = next(
        e for e in router.log(conn)["events"] if e["event"] == "approval.requested"
    )
    assert ev["detail"]["capability"] == "deploy"
    json.dumps(ev)


# idle wake: wake_claim / release_wake_claims

LONG_AGO = "2000-01-01T00:00:00.000Z"
FAR_FUTURE = "9999-01-01T00:00:00.000Z"


def stamped(conn, request_id):
    row = conn.execute(
        "SELECT stop_blocked_at FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return row["stop_blocked_at"]


def test_wake_claim_stamps_once_and_leaves_status_queued(conn, world):
    r = ask(conn, world)
    assert router.wake_claim(conn, r["request_id"]) is True
    assert stamped(conn, r["request_id"]) is not None
    assert router.get_request(conn, r["request_id"])["status"] == "queued"
    assert router.wake_claim(conn, r["request_id"]) is False


def test_wake_claim_refuses_a_request_that_is_not_queued(conn, world):
    r = ask(conn, world, "deploy")  # awaiting_grant
    assert router.wake_claim(conn, r["request_id"]) is False
    assert stamped(conn, r["request_id"]) is None


def test_stop_claim_takes_an_unread_wake_claimed_request_once(conn, world):
    # The wake landed in a busy turn and the model did not act on it: the end of that
    # turn blocks once with the request instead of leaving it for the 10-minute release.
    r = ask(conn, world)
    router.wake_claim(conn, r["request_id"])
    got = router.stop_claim(conn, world["bob"])
    assert [(g["request_id"], g["status"]) for g in got] == [
        (r["request_id"], "queued")
    ]
    assert "request.stop_claimed" in audit_events(conn, r["request_id"])
    assert stamped(conn, r["request_id"]) is not None
    assert router.stop_claim(conn, world["bob"]) == []


def test_a_stop_claimed_wake_is_released_and_offered_again(conn, world):
    r = ask(conn, world)
    router.wake_claim(conn, r["request_id"])
    router.stop_claim(conn, world["bob"])
    assert router.release_wake_claims(conn, FAR_FUTURE) == [r["request_id"]]
    assert stamped(conn, r["request_id"]) is None
    got = router.stop_claim(conn, world["bob"])
    assert [g["request_id"] for g in got] == [r["request_id"]]
    assert "request.wake_released" in audit_events(conn, r["request_id"])


def test_stop_claim_leaves_a_read_wake_alone(conn, world):
    r = ask(conn, world)
    router.wake_claim(conn, r["request_id"])
    router.inbox(conn, world["bob"])
    assert router.stop_claim(conn, world["bob"]) == []


def test_release_only_takes_claims_older_than_the_cutoff(conn, world):
    r = ask(conn, world)
    router.wake_claim(conn, r["request_id"])
    assert router.release_wake_claims(conn, LONG_AGO) == []
    assert stamped(conn, r["request_id"]) is not None


def test_an_inbox_read_acks_the_wake_so_release_leaves_it(conn, world):
    r = ask(conn, world)
    router.wake_claim(conn, r["request_id"])
    assert [g["request_id"] for g in router.inbox(conn, world["bob"])] == [
        r["request_id"]
    ]
    assert router.release_wake_claims(conn, FAR_FUTURE) == []
    assert router.get_request(conn, r["request_id"])["status"] == "delivered"
    assert stamped(conn, r["request_id"]) is not None


def test_stop_claim_stamps_but_leaves_the_request_queued(conn, world):
    r = ask(conn, world)
    got = router.stop_claim(conn, world["bob"])
    assert [(g["request_id"], g["status"]) for g in got] == [
        (r["request_id"], "queued")
    ]
    assert stamped(conn, r["request_id"]) is not None
    assert "request.delivered" not in audit_events(conn, r["request_id"])
    assert router.stop_claim(conn, world["bob"]) == []


def test_an_inbox_read_delivers_a_stop_claimed_request_so_release_leaves_it(
    conn, world
):
    r = ask(conn, world)
    router.stop_claim(conn, world["bob"])
    router.inbox(conn, world["bob"])
    assert router.get_request(conn, r["request_id"])["status"] == "delivered"
    assert router.release_wake_claims(conn, FAR_FUTURE) == []


def test_an_unread_stop_claim_is_released_and_offered_again(conn, world):
    # The hook may die after the claim and before it prints the block.
    r = ask(conn, world)
    router.stop_claim(conn, world["bob"])
    assert router.release_wake_claims(conn, LONG_AGO) == []
    assert router.release_wake_claims(conn, FAR_FUTURE) == [r["request_id"]]
    again = router.stop_claim(conn, world["bob"])
    assert [g["request_id"] for g in again] == [r["request_id"]]


def test_release_never_touches_worker_claimed_rows(conn, world, node):
    c = ask(conn, world)
    assert router.claim_for_worker(
        conn, c["request_id"], store.daemon_actor(node.node_id)
    )
    assert router.release_wake_claims(conn, FAR_FUTURE) == []
    assert stamped(conn, c["request_id"]) is not None


def test_release_wake_claim_frees_one_request_at_once(conn, world):
    a, b = ask(conn, world), ask(conn, world)
    router.wake_claim(conn, a["request_id"])
    router.wake_claim(conn, b["request_id"])
    assert router.release_wake_claim(conn, a["request_id"]) is True
    assert router.release_wake_claim(conn, a["request_id"]) is False
    assert stamped(conn, a["request_id"]) is None
    assert stamped(conn, b["request_id"]) is not None


def approve_once(conn, world, request):
    return router.answer_approval(conn, request["request_id"], "once", world["human"])


def test_once_approval_does_not_outlive_an_expired_request(conn, world):
    a = ask(conn, world, "deploy", timeout_s=1)
    approve_once(conn, world, a)
    router.expire_due(conn, "9999-01-01T00:00:00.000Z")
    assert router.get_request(conn, a["request_id"])["status"] == "expired"
    assert grants.list_grants(conn, world["alice"], world["bob"]) == []
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"


def test_once_approval_does_not_outlive_a_failed_worker(conn, world):
    a = ask(conn, world, "deploy")
    approve_once(conn, world, a)
    assert router.fail_open(conn, a["request_id"], "worker_lost", world["bob"])
    assert grants.list_grants(conn, world["alice"], world["bob"]) == []
    assert ask(conn, world, "deploy")["status"] == "awaiting_grant"


def test_once_approval_is_used_up_by_the_done_reply(conn, world):
    a = ask(conn, world, "deploy")
    approve_once(conn, world, a)
    router.reply(conn, a["request_id"], world["bob"], "ok", "done")
    assert grants.list_grants(conn, world["alice"], world["bob"]) == []


def test_once_approval_of_a_second_request_leaves_the_first_one_valid(conn, world):
    first = ask(conn, world, "deploy")
    second = ask(conn, world, "deploy")
    approve_once(conn, world, first)
    approve_once(conn, world, second)
    done1 = router.reply(conn, first["request_id"], world["bob"], "x", "done")
    done2 = router.reply(conn, second["request_id"], world["bob"], "x", "done")
    assert (done1["status"], done2["status"]) == ("done", "done")
    assert grants.list_grants(conn, world["alice"], world["bob"]) == []


def test_once_approval_covers_only_its_own_request(conn, world):
    a = ask(conn, world, "deploy")
    approve_once(conn, world, a)
    b = ask(conn, world, "deploy")
    assert b["status"] == "awaiting_grant"
