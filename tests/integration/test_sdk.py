"""The Python SDK against a real daemon: join, answer, fail softly, ask."""

import importlib.util
import threading
import time
from pathlib import Path

import httpx
import pytest

from covey.sdk import Agent, DaemonError

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/python-agent/agent.py"


def make_agent(daemon, slug, **kw):
    return Agent(
        slug,
        summary=f"agent {slug}",
        capabilities=[{"name": "explain", "level": "open"}],
        base_url=daemon.base_url,
        join_token_path=daemon.cfg.join_token_path,
        poll_s=0.05,
        **kw,
    )


def background(agent, handler):
    thread = threading.Thread(target=agent.serve, args=(handler,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while agent.token is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return thread


@pytest.fixture
def asker(daemon):
    a = make_agent(daemon, "asker")
    a.register()
    yield a
    a.close()


def test_serve_answers_a_request(daemon, asker):
    worker = make_agent(daemon, "worker")
    thread = background(worker, lambda req: f"answer to: {req['task']}")
    try:
        req = asker.ask("worker", "what is 2+2?", "explain")
        done = asker.wait(req["request_id"], timeout_s=10)
        assert done["status"] == "done"
        assert "what is 2+2?" in done["result"]
    finally:
        worker.stop()
        thread.join(5)
    assert not thread.is_alive()


def test_handler_exception_becomes_failed_reply(daemon, asker):
    worker = make_agent(daemon, "worker")

    def boom(req):
        raise RuntimeError("kaput")

    thread = background(worker, boom)
    try:
        req = asker.ask("worker", "anything", "explain")
        assert asker.wait(req["request_id"], timeout_s=10)["status"] == "failed"
        assert thread.is_alive()  # the loop survived
        again = asker.ask("worker", "again", "explain")
        assert asker.wait(again["request_id"], timeout_s=10)["status"] == "failed"
    finally:
        worker.stop()
        thread.join(5)


def test_agent_registers_with_poll_delivery_and_is_found(daemon, asker):
    worker = make_agent(daemon, "worker")
    worker.register()
    try:
        cards = {m["card"]["slug"]: m["card"] for m in asker.find()}
        assert {"asker", "worker"} <= set(cards)
        assert cards["worker"]["delivery"] == "poll"
    finally:
        worker.close()


def test_heartbeat_thread_keeps_the_agent_live(daemon):
    agent = make_agent(daemon, "beater", heartbeat_s=0.1)
    thread = background(agent, lambda req: "ok")
    try:
        time.sleep(0.5)
        r = httpx.post(
            f"{daemon.base_url}/v1/agents/beater/heartbeat",
            headers={"Authorization": f"Bearer {agent.token}"},
        )
        assert r.json()["liveness"] == "live"
    finally:
        agent.stop()
        thread.join(5)


def test_missing_join_token_gives_a_next_step(daemon, tmp_path):
    agent = Agent("x", base_url=daemon.base_url, join_token_path=tmp_path / "nope")
    with pytest.raises(DaemonError) as exc:
        agent.register()
    assert exc.value.next_step


def test_example_agent_answers_a_request(daemon, asker):
    spec = importlib.util.spec_from_file_location("example_agent", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(EXAMPLE.read_text().splitlines()) < 40
    agent = module.make_agent(
        base_url=daemon.base_url,
        join_token_path=daemon.cfg.join_token_path,
        poll_s=0.05,
    )
    thread = background(agent, module.handle)
    try:
        req = asker.ask(agent.slug, "hello there", "echo")
        done = asker.wait(req["request_id"], timeout_s=10)
        assert done["status"] == "done"
        assert "hello there" in done["result"]
    finally:
        agent.stop()
        thread.join(5)
