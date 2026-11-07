"""Headless workers end to end: a real daemon spawns the stub `claude` for a wakeable agent."""

import contextlib
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from covey.config import Config
from tests.integration.conftest import free_port, run_daemon

STUB = Path(__file__).parent.parent / "fixtures" / "worker" / "stub_claude.py"
BANNED = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "bypassPermissions",
)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class Mesh:
    """A running daemon whose `claude` is the stub in `mode`, with `web` registered."""

    def __init__(self, cfg: Config, running, stub_log: Path, workdir: Path):
        self.cfg, self.daemon, self.stub_log, self.workdir = (
            cfg,
            running,
            stub_log,
            workdir,
        )
        self.tcp = httpx.Client(base_url=running.base_url, timeout=15)
        self.human = httpx.Client(
            transport=httpx.HTTPTransport(uds=running.socket_path),
            base_url="http://covey",
            headers=auth(running.human_token),
            timeout=15,
        )
        r = self.tcp.post(
            "/v1/agents/register",
            json={
                "slug": "web",
                "kind": "python",
                "delivery": "poll",
                "capabilities": [{"name": "explain", "level": "open"}],
            },
            headers=auth(running.join_token),
        )
        self.web = auth(r.json()["agent_token"])

    def add_docs(self, level="open", **worker):
        body = {
            "slug": "docs",
            "workdir": str(self.workdir),
            "wakeable": True,
            "capabilities": [{"name": "explain", "level": level}],
        }
        if worker:
            body["worker"] = worker
        r = self.human.post("/v1/agents", json=body)
        assert r.status_code == 201, r.text

    def ask(self, task="what is X?", headers=None):
        r = self.tcp.post(
            "/v1/requests",
            json={"to": "docs", "capability": "explain", "task": task},
            headers=headers or self.web,
        )
        assert r.status_code == 201, r.text
        return r.json()["request"]

    def wait_final(self, rid, timeout=30):
        r = self.tcp.get(
            f"/v1/requests/{rid}", params={"wait": timeout}, headers=self.web
        )
        return r.json()["request"]

    def status(self, rid):
        return self.tcp.get(f"/v1/requests/{rid}", headers=self.web).json()["request"][
            "status"
        ]

    def wait_status(self, rid, wanted, timeout=15):
        deadline = time.monotonic() + timeout
        while self.status(rid) != wanted:
            assert time.monotonic() < deadline, f"never reached {wanted}"
            time.sleep(0.05)

    def events(self, event):
        found = self.human.get("/v1/log", params={"limit": 500}).json()["events"]
        return [e for e in found if e["event"] == event]

    def wait_event(self, event, timeout=15):
        deadline = time.monotonic() + timeout
        while not (found := self.events(event)):
            assert time.monotonic() < deadline, f"no {event} event"
            time.sleep(0.05)
        return found

    def stub_runs(self):
        if not self.stub_log.exists():
            return []
        return [json.loads(line) for line in self.stub_log.read_text().splitlines()]

    def close(self):
        self.tcp.close()
        self.human.close()


def stub_config(home: Path, tmp_path: Path, mode: str, **overrides) -> Config:
    wrapper = tmp_path / f"claude-{mode}"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"STUB_MODE={mode} STUB_LOG={tmp_path / 'stub.log'} "
        f'exec {sys.executable} {STUB} "$@"\n'
    )
    wrapper.chmod(0o755)
    cfg = Config(
        home=home, port=free_port(), sweep_interval_s=0.2, claude_bin=str(wrapper)
    )
    return replace(cfg, **overrides)


@pytest.fixture
def mesh(short_home, tmp_path, request):
    mode, overrides = (
        request.param
        if isinstance(request.param, tuple)
        else (
            request.param,
            {},
        )
    )
    workdir = tmp_path / "docs-repo"
    workdir.mkdir()
    cfg = stub_config(short_home / "h", tmp_path, mode, **overrides)
    with contextlib.ExitStack() as stack:
        running = stack.enter_context(run_daemon(cfg))
        m = Mesh(cfg, running, tmp_path / "stub.log", workdir)
        m.stop_daemon = (
            stack.close
        )  # for tests that stop it early; closing twice is a no-op
        stack.callback(m.close)
        yield m


def modes(*names):
    return pytest.mark.parametrize("mesh", names, indirect=True)


@modes("reply")
def test_a_wakeable_agent_is_served_by_a_worker_that_replies_over_mcp(mesh):
    mesh.add_docs()
    rid = mesh.ask()["request_id"]
    mesh.wait_status(rid, "working")
    done = mesh.wait_final(rid)
    assert (done["status"], done["result"]) == ("done", "via mcp")

    [run] = mesh.stub_runs()
    assert run["cwd"] == str(mesh.workdir)
    assert "from another agent, not from your user" in run["stdin"]
    assert "what is X?" in run["stdin"]
    assert not any(b in a for a in run["argv"] for b in BANNED)
    assert [e["subject"] for e in mesh.events("worker.spawned")] == [rid]
    [finished] = mesh.wait_event("worker.finished")  # the reply came before the exit
    assert finished["detail"]["cost_usd"] == 0.01
    assert finished["detail"]["session_id"] == "stub-session"
    assert finished["detail"]["replied_by_worker"] is True
    assert not (mesh.cfg.workers_path / rid).exists()  # mcp.json and its token are gone


@modes("silent-success")
def test_a_worker_that_does_not_reply_is_answered_from_its_result(mesh):
    mesh.add_docs()
    done = mesh.wait_final(mesh.ask()["request_id"])
    assert (done["status"], done["result"]) == ("done", "silent answer")
    [finished] = mesh.wait_event("worker.finished")
    assert finished["detail"]["replied_by_worker"] is False


@pytest.mark.parametrize(
    ("mesh", "code"),
    [("max-budget", "worker_error_max_budget_usd"), ("crash", "worker_error")],
    indirect=["mesh"],
)
def test_a_failed_worker_fails_the_request_with_its_reason(mesh, code):
    mesh.add_docs()
    done = mesh.wait_final(mesh.ask()["request_id"])
    assert (done["status"], done["result"]) == ("failed", code)


@pytest.mark.parametrize("mesh", [("hang", {"worker_timeout_s": 1.0})], indirect=True)
def test_a_hung_worker_is_killed_at_the_timeout(mesh):
    mesh.add_docs()
    done = mesh.wait_final(mesh.ask()["request_id"])
    assert (done["status"], done["result"]) == ("failed", "worker_timeout")
    [run] = mesh.stub_runs()
    with pytest.raises(ProcessLookupError):
        os.kill(run["pid"], 0)


@modes("ask-peer")
def test_a_workers_question_continues_the_thread_it_serves(mesh):
    mesh.add_docs()
    original = mesh.ask()
    mesh.wait_final(original["request_id"])
    inbox = mesh.tcp.get("/v1/agents/web/inbox", headers=mesh.web).json()["requests"]
    [sub] = [r for r in inbox if r["to_agent"].startswith("web@")]
    assert sub["parent_id"] == original["request_id"]
    assert sub["thread_id"] == original["thread_id"]
    assert sub["hops"] == original["hops"] + 1


@modes("silent-success")
def test_the_spawn_budget_fails_the_next_request_at_once(mesh):
    mesh.add_docs(daily_spawns=1)
    first = mesh.wait_final(mesh.ask()["request_id"])
    assert first["status"] == "done"
    second = mesh.wait_final(mesh.ask()["request_id"])
    assert (second["status"], second["result"]) == ("failed", "spawn_budget_exhausted")
    assert len(mesh.stub_runs()) == 1


@modes("silent-success")
def test_an_approved_request_is_spawned(mesh):
    mesh.add_docs(level="gated")
    rid = mesh.ask()["request_id"]
    assert mesh.status(rid) == "awaiting_grant"
    r = mesh.human.post(f"/v1/approvals/{rid}", json={"scope": "once"})
    assert r.status_code == 200, r.text
    assert mesh.wait_final(rid)["status"] == "done"


@modes("hang")
def test_stopping_the_daemon_kills_workers_and_a_restart_marks_them_lost(
    mesh, short_home
):
    mesh.add_docs()
    rid = mesh.ask()["request_id"]
    mesh.wait_status(rid, "working")
    deadline = time.monotonic() + 15
    while not mesh.stub_runs():
        assert time.monotonic() < deadline, "the stub never started"
        time.sleep(0.05)
    [run] = mesh.stub_runs()
    mesh.stop_daemon()
    with pytest.raises(ProcessLookupError):
        os.kill(run["pid"], 0)
    with run_daemon(replace(mesh.cfg, port=free_port())) as again:
        h = httpx.Client(
            transport=httpx.HTTPTransport(uds=again.socket_path),
            base_url="http://covey",
            headers=auth(again.human_token),
        )
        req = h.get(f"/v1/requests/{rid}").json()["request"]
        h.close()
    assert (req["status"], req["result"]) == ("failed", "worker_lost")
