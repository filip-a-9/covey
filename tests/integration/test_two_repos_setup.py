"""examples/two-repos/setup.sh builds the demo repos and cards; it never runs `claude`."""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from covey.cli.client import human_client
from covey.config import Config

HERE = Path(__file__).resolve().parents[2] / "examples" / "two-repos"
COVEY = Path(sys.executable).parent / "covey"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def demo():
    """A short throwaway dir (unix socket paths are length-limited) and the env for it."""
    root = Path(tempfile.mkdtemp(prefix="covey-demo-", dir="/tmp"))
    env = {
        **os.environ,
        "COVEY_BIN": str(COVEY),
        "COVEY_PORT": str(free_port()),
        "COVEY_CLAUDE": "/nonexistent/claude-is-disabled-in-tests",
        "COVEY_NOTIFY": "log",
        "PATH": f"{COVEY.parent}:{os.environ['PATH']}",
    }
    yield root, env
    subprocess.run(
        [str(COVEY), "down"],
        check=False,
        env={**env, "COVEY_HOME": str(root / ".covey")},
        capture_output=True,
    )
    shutil.rmtree(root, ignore_errors=True)


def sh(script, root, env, *args):
    res = subprocess.run(
        ["bash", str(HERE / script), str(root), *args],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return res


def covey_json(root, env, *args):
    res = subprocess.run(
        [str(COVEY), *args, "--json"],
        check=False,
        env={**env, "COVEY_HOME": str(root / ".covey")},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return json.loads(res.stdout)


def go_live(root, env, slug):
    """What a Claude Code session's shim does at start: register, with no description."""
    home = root / ".covey"
    token = (home / "join-token").read_text().strip()
    # The human re-adds the agent with its card as it is: a join-token takeover needs that.
    with sqlite3.connect(
        Config(home=home, port=int(env["COVEY_PORT"])).db_path
    ) as db:  # `ps` hides gone agents
        card = json.loads(
            db.execute("SELECT card FROM agents WHERE slug = ?", (slug,)).fetchone()[0]
        )
    keep = ("slug", "kind", "summary", "tags", "capabilities", "workdir", "wakeable")
    with human_client(Config(home=home, port=int(env["COVEY_PORT"]))) as human:
        body = {k: card[k] for k in keep if k in card}
        assert human.post("/v1/agents", json=body).status_code == 201
    r = httpx.post(
        f"http://127.0.0.1:{env['COVEY_PORT']}/v1/agents/register",
        json={
            "slug": slug,
            "kind": "claude-code",
            "delivery": "hooks",
            "session_id": str(uuid.uuid4()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


def test_repos_and_history(demo):
    root, env = demo
    sh("setup.sh", root, env)
    for name in ("api", "web"):
        assert (root / name / ".git").is_dir()
    last = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        check=False,
        cwd=root / "api",
        capture_output=True,
        text=True,
    ).stdout
    assert "currency" in last
    assert "require" in last.lower()
    log = subprocess.run(
        ["git", "log", "--format=%an <%ae>"],
        check=False,
        cwd=root / "web",
        capture_output=True,
        text=True,
    ).stdout
    assert set(log.split("\n")) <= {"Demo <demo@example.invalid>", ""}
    assert (root / "env.sh").is_file()
    unit = subprocess.run(
        [sys.executable, "-m", "unittest"],
        cwd=root / "api",
        capture_output=True,
        check=False,
    )
    assert unit.returncode == 0, unit.stderr
    client = subprocess.run(
        [sys.executable, "-m", "unittest"],
        cwd=root / "web",
        capture_output=True,
        check=False,
    )
    assert client.returncode == 0, client.stderr


def test_init_keeps_hooks_and_allows_mcp_tools(demo):
    root, env = demo
    sh("setup.sh", root, env)
    for name in ("api", "web"):
        settings = json.loads((root / name / ".claude" / "settings.json").read_text())
        assert set(settings["hooks"]) >= {"SessionStart", "Stop", "PostToolUse"}
        assert "mcp__covey" in settings["permissions"]["allow"]
        mcp = json.loads((root / name / ".mcp.json").read_text())
        assert "covey" in mcp["mcpServers"]


def test_setup_is_idempotent(demo):
    root, env = demo
    sh("setup.sh", root, env)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root / "api",
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    sh("setup.sh", root, env)
    again = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root / "api",
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert head == again


def test_card_survives_a_session_registering_and_ranks(demo):
    root, env = demo
    sh("setup.sh", root, env)
    go_live(root, env, "api")
    go_live(root, env, "web")
    agents = {c["slug"]: c for c in covey_json(root, env, "ps")["agents"]}
    caps = {c["name"]: c["level"] for c in agents["api"]["capabilities"]}
    assert caps == {"explain": "open", "search": "open", "run_tests": "gated"}
    assert agents["api"]["summary"].startswith("Owns the orders API")
    assert "web" in agents
    ranked = covey_json(root, env, "find", "owns the orders API")["matches"]
    assert ranked[0]["card"]["slug"] == "api"
