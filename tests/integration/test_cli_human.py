"""The human CLI commands against a real daemon (in-process, over the unix socket)."""

import json
import threading
import time

import httpx
import pytest
from typer.testing import CliRunner

from covey.cli import app


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(daemon, monkeypatch):
    monkeypatch.setenv("COVEY_HOME", str(daemon.cfg.home))
    monkeypatch.setenv("COVEY_PORT", str(daemon.cfg.port))


@pytest.fixture
def tcp(daemon):
    with httpx.Client(base_url=daemon.base_url, timeout=15) as c:
        yield c


def register(tcp, daemon, slug, caps=(), summary=None):
    body = {
        "slug": slug,
        "kind": "python",
        "delivery": "poll",
        "summary": summary or f"agent {slug}",
        "capabilities": [{"name": n, "level": lvl} for n, lvl in caps],
    }
    r = tcp.post("/v1/agents/register", json=body, headers=auth(daemon.join_token))
    assert r.status_code == 200, r.text
    return auth(r.json()["agent_token"])


@pytest.fixture
def web(tcp, daemon):
    return register(tcp, daemon, "web", [("explain", "open")])


@pytest.fixture
def api(tcp, daemon):
    caps = [("explain", "open"), ("run_tests", "gated")]
    return register(tcp, daemon, "api", caps, "serves the billing cache")


def cli(*args, expect=0):
    res = CliRunner().invoke(app, list(args))
    assert res.exit_code == expect, res.output
    return res


def cli_json(*args, expect=0):
    return json.loads(cli(*args, "--json", expect=expect).output)


def gated_request(tcp, web):
    r = tcp.post(
        "/v1/requests",
        json={"to": "api", "capability": "run_tests", "task": "run the suite"},
        headers=web,
    )
    assert r.status_code == 201, r.text
    return r.json()["request"]["request_id"]


def test_ps_lists_agents(env, web, api):
    data = cli_json("ps")
    assert {a["slug"] for a in data["agents"]} == {"web", "api"}
    text = cli("ps").output
    assert "web" in text and "api" in text


def test_find_ranks_by_need(env, web, api):
    data = cli_json("find", "billing cache")
    assert data["matches"][0]["card"]["slug"] == "api"
    assert data["matches"][0]["why"]
    assert "api" in cli("find", "billing cache").output


def test_ask_creates_a_request_from_the_human(env, web, api):
    req = cli_json("ask", "api", "explain the cache", "--cap", "explain")["request"]
    assert req["from_agent"].startswith("human@")
    assert req["status"] in ("queued", "delivered")


def test_inbox_shows_what_is_waiting_without_delivering_it(env, tcp, web, api):
    cli("ask", "api", "explain the cache")
    first = cli_json("inbox", "api")["requests"]
    assert [r["task"] for r in first] == ["explain the cache"]
    assert cli_json("inbox", "api")["requests"][0]["status"] == "queued"


def test_ask_wait_returns_the_reply(env, tcp, web, api):
    def replier():
        for _ in range(100):
            reqs = tcp.get("/v1/agents/api/inbox", headers=api).json()["requests"]
            if reqs:
                tcp.post(
                    f"/v1/requests/{reqs[0]['request_id']}/reply",
                    json={"result": "it is LRU", "status": "done"},
                    headers=api,
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=replier)
    t.start()
    data = cli_json("ask", "api", "explain the cache", "--wait")
    t.join()
    assert data["request"]["status"] == "done"
    assert data["request"]["result"] == "it is LRU"


def test_ask_wait_times_out_with_nonzero_exit(env, web, api):
    cli("ask", "api", "explain", "--wait", "--timeout", "1", expect=1)


def test_ask_unknown_agent_fails(env, web):
    cli("ask", "nobody", "hello", expect=1)


def test_describe_edits_another_agents_card(env, web, api):
    data = cli_json(
        "describe", "api", "--summary", "now does auth", "--tags", "auth,jwt"
    )
    assert data["card"]["summary"] == "now does auth"
    assert data["card"]["tags"] == ["auth", "jwt"]


def test_describe_without_fields_fails(env, api):
    cli("describe", "api", expect=1)


def test_approve_once_queues_the_request_and_creates_a_grant(env, tcp, web, api):
    rid = gated_request(tcp, web)
    pending = cli_json("approvals")["requests"]
    assert [r["request_id"] for r in pending] == [rid]
    data = cli_json("approve", rid, "--once")
    assert data["request"]["status"] == "queued"
    assert data["grant"]["scope"] == "once"
    assert cli_json("approvals")["requests"] == []


def test_deny_refuses_the_request(env, tcp, web, api):
    rid = gated_request(tcp, web)
    data = cli_json("deny", rid)
    assert data["request"]["status"] == "refused"


def test_approve_with_two_scopes_fails(env, tcp, web, api):
    rid = gated_request(tcp, web)
    cli("approve", rid, "--once", "--always", expect=1)


def test_grant_list_and_revoke(env, web, api):
    g = cli_json("grant", "web", "api", "run_tests", "--always")["grant"]
    assert g["scope"] == "always"
    assert [x["grant_id"] for x in cli_json("grants")["grants"]] == [g["grant_id"]]
    cli("revoke", g["grant_id"])
    assert cli_json("grants")["grants"] == []


def test_grant_requires_exactly_one_scope(env, web, api):
    cli("grant", "web", "api", "run_tests", expect=1)


def test_revoke_unknown_grant_says_what_to_do(env, web):
    res = cli("revoke", "01J00000000000000000000001", expect=1)
    assert "covey grants" in res.output


def test_commands_when_daemon_is_down_say_covey_up(tmp_path, monkeypatch):
    monkeypatch.setenv("COVEY_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("COVEY_PORT", "7999")
    for args in (["ps"], ["approvals"], ["grants"], ["find", "x"]):
        res = cli(*args, expect=1)
        assert "covey up" in res.output


def doctor(*args):
    res = CliRunner().invoke(app, ["doctor", "--json", *args])
    return res, {c["name"]: c for c in json.loads(res.output)["checks"]}


def test_doctor_passes_the_checks_a_healthy_setup_controls(
    env, daemon, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cli("init", "--user")
    _, checks = doctor()
    for name in ("socket", "tokens", "hooks", "mcp", "port"):
        assert checks[name]["ok"], checks[name]


def test_doctor_reports_each_failure_with_a_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("COVEY_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("COVEY_PORT", "7999")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    res, checks = doctor()
    assert res.exit_code == 1
    for name in ("socket", "tokens", "hooks", "mcp"):
        assert checks[name]["ok"] is False
        assert checks[name]["fix"]


def test_doctor_flags_loose_token_permissions(env, daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    daemon.cfg.join_token_path.chmod(0o644)
    _, checks = doctor()
    assert checks["tokens"]["ok"] is False
    assert "chmod 600" in checks["tokens"]["fix"]


def test_doctor_flags_a_port_held_by_something_else(env, daemon, tmp_path, monkeypatch):
    # nothing answers on the socket (wrong home) but the daemon's port is taken
    monkeypatch.setenv("COVEY_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _, checks = doctor()
    assert checks["port"]["ok"] is True  # a coveyd answers on it: fine
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        monkeypatch.setenv("COVEY_PORT", str(s.getsockname()[1]))
        _, checks = doctor()
    assert checks["port"]["ok"] is False
    assert checks["port"]["fix"]


# ------------------------------------------------------- covey agent add


def test_agent_add_pre_registers_a_wakeable_agent(env, tmp_path):
    data = cli_json(
        "agent",
        "add",
        "docs",
        "--workdir",
        str(tmp_path),
        "--wakeable",
        "--allowed-tools",
        "Read,Edit",
        "--max-turns",
        "4",
        "--daily-spawns",
        "3",
    )
    card = data["card"]
    assert card["liveness"] == "wakeable"
    assert card["workdir"] == str(tmp_path)
    assert card["worker"] == {
        "allowed_tools": ["Read", "Edit"],
        "max_turns": 4,
        "daily_spawns": 3,
    }
    assert [a["slug"] for a in cli_json("ps")["agents"]] == ["docs"]


def test_agent_add_resolves_a_relative_workdir(env, tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path)
    card = cli_json("agent", "add", "docs", "--workdir", "repo")["card"]
    assert card["workdir"] == str(tmp_path / "repo")


def test_agent_add_missing_workdir_fails(env, tmp_path):
    cli("agent", "add", "docs", "--workdir", str(tmp_path / "nope"), expect=1)


def test_agent_add_with_bash_fails_with_the_daemons_reason(env, tmp_path):
    data = cli_json(
        "agent",
        "add",
        "docs",
        "--workdir",
        str(tmp_path),
        "--allowed-tools",
        "Bash",
        expect=1,
    )
    assert data["code"] == "tool_not_allowed"
