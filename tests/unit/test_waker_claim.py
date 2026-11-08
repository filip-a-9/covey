"""waker.claim: which queued requests get a worker, and which fail at once."""

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from covey.daemon import peer, registry, router, store, waker
from tests.conftest import add_agent

EXPLAIN = [{"name": "explain", "level": "open"}]


@pytest.fixture
def wcfg(cfg):
    return replace(cfg, claude_bin=sys.executable)


@pytest.fixture
def web(conn, node):
    return add_agent(conn, node, "web")


def pre_register(conn, node, slug, tmp_path, **worker):
    workdir = tmp_path / slug
    workdir.mkdir(exist_ok=True)
    body = {
        "slug": slug,
        "workdir": str(workdir),
        "wakeable": True,
        "capabilities": EXPLAIN,
    }
    if worker:
        body["worker"] = worker
    registry.create_agent(conn, node.node_id, body)
    return f"{slug}@{node.node_id}"


def ask(conn, web, slug, task="what is X?"):
    return router.create_request(conn, web, slug, "explain", task)["request_id"]


def status(conn, rid):
    req = router.get_request(conn, rid)
    return req["status"], req["result"]


def claim(conn, wcfg, running=(), max_workers=2):
    return waker.claim(conn, replace(wcfg, max_workers=max_workers), set(running))


def events(conn, event):
    return conn.execute(
        "SELECT subject, detail FROM audit WHERE event = ? ORDER BY id", (event,)
    ).fetchall()


def test_a_queued_request_to_a_wakeable_agent_is_claimed(
    conn, node, wcfg, web, tmp_path
):
    docs = pre_register(conn, node, "docs", tmp_path)
    rid = ask(conn, web, "docs")
    claims = claim(conn, wcfg)
    assert [(c.request["request_id"], c.agent_id) for c in claims] == [(rid, docs)]
    assert claims[0].workdir == str(tmp_path / "docs")
    assert status(conn, rid) == ("working", None)
    row = conn.execute(
        "SELECT stop_blocked_at FROM requests WHERE request_id = ?", (rid,)
    ).fetchone()
    assert row["stop_blocked_at"] is not None
    assert [e["subject"] for e in events(conn, "worker.spawned")] == [rid]


def test_claimed_requests_are_not_claimed_again(conn, node, wcfg, web, tmp_path):
    pre_register(conn, node, "docs", tmp_path)
    ask(conn, web, "docs")
    claim(conn, wcfg)
    assert claim(conn, wcfg) == []


def test_a_live_agent_is_left_to_its_session(conn, node, wcfg, web, tmp_path):
    pre_register(conn, node, "docs", tmp_path)
    registry.register(
        conn,
        node.node_id,
        {
            "slug": "docs",
            "kind": "claude-code",
            "delivery": "hooks",
            "capabilities": EXPLAIN,
        },
        peer.Origin(frozenset(), str(tmp_path / "docs")),
    )
    rid = ask(conn, web, "docs")
    assert claim(conn, wcfg) == []
    assert status(conn, rid) == ("queued", None)


def test_one_worker_per_agent(conn, node, wcfg, web, tmp_path):
    docs = pre_register(conn, node, "docs", tmp_path)
    first, second = ask(conn, web, "docs"), ask(conn, web, "docs")
    assert [c.request["request_id"] for c in claim(conn, wcfg)] == [first]
    assert status(conn, second) == ("queued", None)
    router.fail_open(conn, first, "test", "x")  # the first worker is over...
    assert (
        claim(conn, wcfg, running=[docs]) == []
    )  # ...but its process is still running
    assert [c.request["request_id"] for c in claim(conn, wcfg)] == [second]


def test_max_workers_across_agents(conn, node, wcfg, web, tmp_path):
    pre_register(conn, node, "docs", tmp_path)
    pre_register(conn, node, "infra", tmp_path)
    ask(conn, web, "docs")
    late = ask(conn, web, "infra")
    assert len(claim(conn, wcfg, max_workers=1)) == 1
    assert status(conn, late) == ("queued", None)
    assert claim(conn, wcfg, running=["x@y"], max_workers=1) == []


def spawned_at(conn, agent_id, when):
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO audit (ts, actor, event, subject, detail) VALUES (?,?,?,?,?)",
            (
                when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "coveyd@x",
                "worker.spawned",
                "r0",
                json.dumps({"agent": agent_id}),
            ),
        )


def test_over_the_daily_budget_fails_at_once(conn, node, wcfg, web, tmp_path):
    docs = pre_register(conn, node, "docs", tmp_path, daily_spawns=1)
    spawned_at(conn, docs, datetime.now(UTC) - timedelta(hours=23))
    rid = ask(conn, web, "docs")
    assert claim(conn, wcfg) == []
    assert status(conn, rid) == ("failed", "spawn_budget_exhausted")
    assert [e["subject"] for e in events(conn, "worker.failed")] == [rid]


def test_spawns_older_than_24_hours_do_not_count(conn, node, wcfg, web, tmp_path):
    docs = pre_register(conn, node, "docs", tmp_path, daily_spawns=1)
    spawned_at(conn, docs, datetime.now(UTC) - timedelta(hours=25))
    ask(conn, web, "docs")
    assert len(claim(conn, wcfg)) == 1


def test_the_default_budget_is_twenty_a_day(conn, node, wcfg, web, tmp_path):
    docs = pre_register(conn, node, "docs", tmp_path)
    for _ in range(19):
        spawned_at(conn, docs, datetime.now(UTC) - timedelta(hours=1))
    ask(conn, web, "docs")
    assert len(claim(conn, wcfg)) == 1
    rid = ask(conn, web, "docs")
    router.fail_open(conn, claim_ids(conn)[0], "test", "x")
    assert claim(conn, wcfg) == []
    assert status(conn, rid) == ("failed", "spawn_budget_exhausted")


def claim_ids(conn):
    rows = conn.execute("SELECT request_id FROM requests WHERE status = 'working'")
    return [r["request_id"] for r in rows]


def test_a_missing_workdir_fails_worker_unavailable(conn, node, wcfg, web, tmp_path):
    pre_register(conn, node, "docs", tmp_path)
    (tmp_path / "docs").rmdir()
    rid = ask(conn, web, "docs")
    assert claim(conn, wcfg) == []
    assert status(conn, rid) == ("failed", "worker_unavailable")


def test_claude_not_found_fails_worker_unavailable(conn, node, wcfg, web, tmp_path):
    pre_register(conn, node, "docs", tmp_path)
    rid = ask(conn, web, "docs")
    gone = replace(wcfg, claude_bin=str(tmp_path / "no-claude"))
    assert claim(conn, gone) == []
    assert status(conn, rid) == ("failed", "worker_unavailable")
    detail = json.loads(events(conn, "worker.failed")[0]["detail"])
    assert "COVEY_CLAUDE" in detail["next_step"]


def test_working_requests_left_by_a_previous_daemon_become_worker_lost(
    conn, node, wcfg, web, tmp_path
):
    pre_register(conn, node, "docs", tmp_path)
    rid = ask(conn, web, "docs")
    claim(conn, wcfg)
    assert waker.recover(conn, node.node_id) == [rid]
    assert status(conn, rid) == ("failed", "worker_lost")


def test_same_millisecond_requests_are_served_in_arrival_order(
    conn, node, wcfg, web, tmp_path
):
    docs = pre_register(conn, node, "docs", tmp_path)
    first, second = "01M6TQG8P3ZZZZZZZZZZZZZZZZ", "01M6TQG8P30000000000000000"
    with store.transaction(conn):
        for rid in (first, second):  # `second` sorts first by id but arrived later
            conn.execute(
                "INSERT INTO requests (request_id, thread_id, from_agent, to_agent,"
                " capability, task, status, created_at, updated_at) VALUES"
                " (?, ?, ?, ?, 'explain', 'x', 'queued', 'T', 'T')",
                (rid, rid, web, docs),
            )
    assert [c.request["request_id"] for c in claim(conn, wcfg)] == [first]


def test_a_wake_claimed_request_gets_no_worker(conn, node, wcfg, web, tmp_path):
    """Idle wake already posted it to a session; a flip to wakeable must not also spawn."""
    pre_register(conn, node, "docs", tmp_path)
    rid = ask(conn, web, "docs")
    assert router.wake_claim(conn, rid)
    assert claim(conn, wcfg) == []
    assert status(conn, rid) == ("queued", None)
    router.release_wake_claims(conn, "9999-01-01T00:00:00.000Z")
    assert [c.request["request_id"] for c in claim(conn, wcfg)] == [rid]
