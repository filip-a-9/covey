"""Session and stop-claim endpoints the Claude Code hooks use, against a real daemon."""

import httpx
import pytest

from tests.integration.procs import register_from


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tcp(daemon):
    with httpx.Client(base_url=daemon.base_url, timeout=15) as c:
        yield c


@pytest.fixture
def human(daemon):
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=daemon.socket_path),
        base_url="http://covey",
        headers=auth(daemon.human_token),
        timeout=15,
    ) as c:
        yield c


def register(tcp, daemon, slug, session_id=None, wakeable=False):
    workdir = daemon.cfg.home.parent / f"wd-{slug}"
    if wakeable:  # only the human token can set it: pre-add the agent, then register
        workdir.mkdir(exist_ok=True)
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=daemon.socket_path),
            base_url="http://covey",
            headers=auth(daemon.human_token),
        ) as human:
            made = human.post(
                "/v1/agents",
                json={"slug": slug, "workdir": str(workdir), "wakeable": True},
            )
            assert made.status_code == 201, made.text
    body = {
        "slug": slug,
        "kind": "claude-code",
        "delivery": "hooks",
        "capabilities": [
            {"name": "explain", "level": "open"},
            {"name": "run_tests", "level": "gated"},
        ],
    }
    if session_id:
        body["session_id"] = session_id
    if wakeable:  # a human-created slug: a real process in its workdir registers
        status, data = register_from(daemon, body, workdir, workdir.parent)
        assert status == 200, data
        return auth(data["agent_token"])
    r = tcp.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
    assert r.status_code == 200, r.text
    return auth(r.json()["agent_token"])


def liveness(tcp, headers):
    return tcp.get("/v1/whoami", headers=headers).json()["liveness"]


def test_a_new_session_id_ends_the_old_sessions_grant(tcp, human, daemon):
    web = register(tcp, daemon, "web")
    api = register(tcp, daemon, "api", session_id="s1")
    grant = {
        "requester": "web",
        "target": "api",
        "capability": "run_tests",
        "scope": "session",
    }
    assert human.post("/v1/grants", json=grant).status_code == 201
    ask = {"to": "api", "capability": "run_tests", "task": "run them"}
    first = tcp.post("/v1/requests", json=ask, headers=web).json()["request"]
    assert first["status"] == "queued"

    r = tcp.put("/v1/agents/api/session", json={"session_id": "s2"}, headers=api)
    assert r.status_code == 200
    assert r.json()["liveness"] == "live"

    second = tcp.post("/v1/requests", json=ask, headers=web).json()["request"]
    assert second["status"] == "awaiting_grant"


def test_session_endpoints_are_own_slug_only(tcp, daemon):
    web = register(tcp, daemon, "web")
    register(tcp, daemon, "api")
    assert (
        tcp.put("/v1/agents/api/session", json={"session_id": "x"}, headers=web)
    ).status_code == 403
    assert (
        tcp.request(
            "DELETE", "/v1/agents/api/session", json={"reason": "x"}, headers=web
        )
    ).status_code == 403
    assert (tcp.post("/v1/agents/api/inbox/stop-claim", headers=web)).status_code == 403
    assert (
        tcp.put("/v1/agents/web/session", json={"session_id": "x"})
    ).status_code == 401


def test_stop_claim_hands_out_each_request_once(tcp, daemon):
    web = register(tcp, daemon, "web")
    api = register(tcp, daemon, "api", session_id="s1")
    ask = {"to": "api", "capability": "explain", "task": "what is this"}
    rid = tcp.post("/v1/requests", json=ask, headers=web).json()["request"][
        "request_id"
    ]

    peek = tcp.get("/v1/agents/api/inbox", params={"peek": "true"}, headers=api)
    assert [r["status"] for r in peek.json()["requests"]] == ["queued"]

    claim = tcp.post("/v1/agents/api/inbox/stop-claim", headers=api)
    got = claim.json()["requests"]
    assert [r["request_id"] for r in got] == [rid]
    assert got[0]["status"] == "queued"  # the model's inbox read delivers it
    assert tcp.post("/v1/agents/api/inbox/stop-claim", headers=api).json() == {
        "covey": "1",
        "requests": [],
    }


def test_stop_claim_skips_requests_the_agent_has_already_seen(tcp, daemon):
    web = register(tcp, daemon, "web")
    api = register(tcp, daemon, "api", session_id="s1")
    ask = {"to": "api", "capability": "explain", "task": "what is this"}
    rid = tcp.post("/v1/requests", json=ask, headers=web).json()["request"][
        "request_id"
    ]

    # Reading the inbox moves the request to delivered: the model has seen it.
    seen = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
    assert [r["request_id"] for r in seen] == [rid]
    assert tcp.post("/v1/agents/api/inbox/stop-claim", headers=api).json() == {
        "covey": "1",
        "requests": [],
    }


def test_ending_the_session_makes_the_agent_wakeable_and_heartbeats_do_not_revive(
    tcp, daemon
):
    api = register(tcp, daemon, "api", session_id="s1", wakeable=True)
    gone = register(tcp, daemon, "gone", session_id="s9")

    r = tcp.request(
        "DELETE", "/v1/agents/api/session", json={"reason": "logout"}, headers=api
    )
    assert r.status_code == 200
    assert r.json()["liveness"] == "wakeable"
    beat = tcp.post("/v1/agents/api/heartbeat", headers=api)
    assert beat.status_code == 200
    assert beat.json()["liveness"] == "wakeable"
    assert liveness(tcp, api) == "wakeable"

    r = tcp.request(
        "DELETE", "/v1/agents/gone/session", json={"reason": "logout"}, headers=gone
    )
    assert r.json()["liveness"] == "gone"

    back = tcp.put("/v1/agents/api/session", json={"session_id": "s2"}, headers=api)
    assert back.json()["liveness"] == "live"
    assert (
        tcp.post("/v1/agents/api/heartbeat", headers=api).json()["liveness"] == "live"
    )


# ---------------------------------------------------------------- covey hook <event>


def run_hook(daemon, event, stdin, claude_pid, extra_args=()):
    import json

    from typer.testing import CliRunner

    from covey.cli import app

    env = {
        "COVEY_HOME": str(daemon.cfg.home),
        "COVEY_PORT": str(daemon.cfg.port),
        "CLAUDE_PID": str(claude_pid),
    }
    result = CliRunner().invoke(
        app, ["hook", event, *extra_args], input=json.dumps(stdin), env=env
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_hooks_drive_a_live_session_through_the_cli(tcp, daemon):
    import os

    from covey.mcp.identity import _start_time, write_session_file

    web = register(tcp, daemon, "web")
    token = register(tcp, daemon, "api", session_id="s1", wakeable=True)
    pid = os.getpid()
    claude = {"pid": pid, "start_time": _start_time(pid)}
    write_session_file(daemon.cfg, claude, "api", token["Authorization"][7:])

    started = run_hook(
        daemon, "session-start", {"session_id": "s2", "cwd": "/x/api"}, pid
    )
    assert "agent `api`" in started["hookSpecificOutput"]["additionalContext"]

    ask = {"to": "api", "capability": "explain", "task": "what is this"}
    tcp.post("/v1/requests", json=ask, headers=web)
    note = run_hook(daemon, "post-tool", {"tool_name": "Bash"}, pid)
    assert "1 request waiting" in note["hookSpecificOutput"]["additionalContext"]
    # Peeking left it queued, so the note repeats until the model picks it up.
    assert run_hook(daemon, "post-tool", {}, pid) == note

    blocked = run_hook(daemon, "stop", {"stop_hook_active": False}, pid)
    assert blocked["decision"] == "block"
    assert blocked["reason"].startswith("The following is a request from another agent")
    assert run_hook(daemon, "stop", {"stop_hook_active": False}, pid) is None

    assert run_hook(daemon, "session-end", {"reason": "clear"}, pid) is None
    assert liveness(tcp, token) == "live"
    run_hook(daemon, "session-end", {"reason": "logout"}, pid)
    assert liveness(tcp, token) == "wakeable"


def test_hooks_stay_silent_without_a_session_file_or_a_daemon(daemon, short_home):
    import os

    assert run_hook(daemon, "stop", {}, os.getpid()) is None
    assert run_hook(daemon, "session-end", {"reason": "logout"}, os.getpid()) is None
    # No daemon at all: still exit 0 with nothing on stdout.
    import json

    from typer.testing import CliRunner

    from covey.cli import app

    env = {"COVEY_HOME": str(short_home / "none"), "CLAUDE_PID": str(os.getpid())}
    r = CliRunner().invoke(app, ["hook", "stop"], input=json.dumps({}), env=env)
    assert (r.exit_code, r.stdout) == (0, "")
