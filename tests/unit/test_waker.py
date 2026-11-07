"""waker: the worker's argv, its mcp.json and reading its result (no processes here)."""

import json
from pathlib import Path

import pytest

from covey.config import Config, load
from covey.daemon import waker

RUNS = Path(__file__).parent.parent / "fixtures" / "worker" / "claude-code-2.1.319"
BANNED = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "bypassPermissions",
)


@pytest.fixture
def cfg(tmp_path):
    return Config(home=tmp_path / "h", port=7777, claude_bin="/opt/claude")


def argv(cfg, worker=None):
    return waker.build_argv(cfg, worker or {}, Path("/w/mcp.json"), "FRAME")


def flag(args, name):
    return args[args.index(name) + 1]


# ------------------------------------------------------------------ config


def test_worker_settings_come_from_the_environment():
    cfg = load({"COVEY_CLAUDE": "/usr/local/bin/claude", "COVEY_WORKER_MODEL": "haiku"})
    assert (cfg.claude_bin, cfg.worker_model) == ("/usr/local/bin/claude", "haiku")


def test_worker_settings_default_to_claude_on_path_and_sonnet():
    cfg = load({})
    assert (cfg.claude_bin, cfg.worker_model) == ("claude", "sonnet")


# -------------------------------------------------------------------- argv


def test_argv_never_bypasses_permissions(cfg):
    args = argv(cfg, {"allowed_tools": ["Read", "Edit"]})
    assert not any(b in a for a in args for b in BANNED)
    assert flag(args, "--permission-prompts") == "none"
    assert "--restricted" in args
    assert "--strict-mcp-config" in args


def test_argv_starts_with_the_configured_claude_in_print_json_mode(cfg):
    args = argv(cfg)
    assert args[0] == "/opt/claude"
    assert args[1:4] == ["-p", "--output-format", "json"]


def test_default_tools_are_read_grep_glob_plus_covey_tools_without_grant(cfg):
    args = argv(cfg)
    assert flag(args, "--tools") == "Read,Grep,Glob"
    allowed = flag(args, "--allowedTools").split(",")
    assert allowed[:3] == ["Read", "Grep", "Glob"]
    covey = {a for a in allowed if a.startswith("mcp__covey__")}
    assert covey == {
        "mcp__covey__covey_reply",
        "mcp__covey__covey_ask",
        "mcp__covey__covey_wait",
        "mcp__covey__covey_find",
        "mcp__covey__covey_whoami",
        "mcp__covey__covey_log",
        "mcp__covey__covey_inbox",
    }


def test_a_scoped_tool_rule_enables_the_bare_tool(cfg):
    args = argv(cfg, {"allowed_tools": ["Read", "Edit(docs/**)", "Edit(README.md)"]})
    assert flag(args, "--tools") == "Read,Edit"
    assert flag(args, "--allowedTools").startswith(
        "Read,Edit(docs/**),Edit(README.md),"
    )


def test_caps_come_from_the_worker_block_or_the_card_defaults(cfg):
    args = argv(cfg)
    assert (flag(args, "--max-turns"), flag(args, "--max-budget-usd")) == ("10", "1.0")
    args = argv(cfg, {"max_turns": 3, "max_budget_usd": 0.5})
    assert (flag(args, "--max-turns"), flag(args, "--max-budget-usd")) == ("3", "0.5")


def test_argv_carries_model_mcp_config_and_frame(cfg):
    args = argv(cfg)
    assert flag(args, "--model") == "sonnet"
    assert flag(args, "--mcp-config") == "/w/mcp.json"
    assert flag(args, "--append-system-prompt") == "FRAME"


# ---------------------------------------------------------------- mcp.json


def test_mcp_config_runs_the_worker_shim_with_identity_and_token(cfg):
    conf = waker.mcp_config(cfg, "docs", "tok")["mcpServers"]["covey"]
    assert conf["args"][-2:] == ["mcp", "--worker"]
    assert conf["env"] == {
        "COVEY_HOME": str(cfg.home),
        "COVEY_PORT": "7777",
        "COVEY_AGENT": "docs",
        "COVEY_WORKER_TOKEN": "tok",
    }


# ------------------------------------------------------------------ result


def run_output(name):
    return (RUNS / name).read_text()


def test_success_with_a_result_is_done_with_that_text():
    out = waker.read_result(run_output("06-bash-write-denied.json"))
    assert out.status == "done"
    assert out.result == json.loads(run_output("06-bash-write-denied.json"))["result"]
    assert out.cost_usd > 0
    assert out.session_id


def test_permission_denials_are_reported():
    out = waker.read_result(run_output("06-bash-write-denied.json"))
    assert [d["tool_name"] for d in out.denials] == ["Bash"]


def test_budget_exhausted_is_failed_with_the_subtype():
    out = waker.read_result(run_output("01-restricted-denied.json"))
    assert (out.status, out.result) == ("failed", "worker_error_max_budget_usd")
    assert out.cost_usd == pytest.approx(0.193, abs=0.001)


def test_success_without_a_result_is_never_done():
    out = waker.read_result(json.dumps({"subtype": "success", "is_error": False}))
    assert (out.status, out.result) == ("failed", "worker_no_result")


@pytest.mark.parametrize("stdout", ["", "Traceback (most recent call last):", "[1, 2]"])
def test_unreadable_output_is_failed_worker_error(stdout):
    out = waker.read_result(stdout)
    assert (out.status, out.result) == ("failed", "worker_error")
    assert out.cost_usd is None


def test_the_argv_runs_the_resolved_claude_not_the_configured_name(cfg):
    args = waker.build_argv(
        cfg, {}, Path("/w/mcp.json"), "FRAME", claude="/abs/bin/claude"
    )
    assert args[0] == "/abs/bin/claude"


def test_recover_kills_workers_a_dead_daemon_left_behind(cfg):
    import os
    import subprocess
    import sys

    from covey import procs
    from covey.daemon import store

    node = store.init_home(cfg)
    conn = store.connect(cfg.db_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    folder = cfg.workers_path / "01JREQ"
    folder.mkdir(parents=True)
    (folder / "pid").write_text(f"{child.pid} {procs._start_time(child.pid)}")
    try:
        waker.recover(conn, node.node_id, cfg.workers_path)
        assert child.wait(timeout=10) != 0  # killed by a signal
        assert not folder.exists()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait()
        conn.close()
