"""The shim's identity: slug, the claude ancestor, and the token file hooks read."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from covey.config import Config
from covey.mcp.identity import (
    IdentityError,
    derive_slug,
    find_claude,
    forget_wake_file,
    read_wake_file,
    wake_file,
    write_session_file,
    write_wake_file,
)


def test_covey_agent_wins_over_the_directory_name():
    assert derive_slug({"COVEY_AGENT": "api-2"}, Path("/code/web")) == "api-2"


@pytest.mark.parametrize(
    ("dirname", "want"),
    [
        ("web", "web"),
        ("My Repo.v2", "my-repo-v2"),
        ("__private", "private"),
        ("a" * 80, "a" * 63),
    ],
)
def test_slug_from_the_directory_name_fits_the_slug_pattern(dirname, want):
    assert derive_slug({}, Path("/code") / dirname) == want


def test_a_directory_name_with_nothing_usable_asks_for_covey_agent():
    with pytest.raises(IdentityError) as exc:
        derive_slug({}, Path("/code/..."))
    assert "COVEY_AGENT" in exc.value.next_step


def test_finds_the_nearest_claude_ancestor(tmp_path):
    claude = tmp_path / "claude"
    claude.symlink_to(sys.executable)
    probe = (
        "import json, os; from covey.mcp.identity import find_claude; "
        "print(json.dumps(find_claude(os.getppid())))"
    )
    # claude -> python (a launcher such as `uv run`) -> python running find_claude.
    # Children get the real interpreter path, or they would be named `claude` too.
    py = sys.executable
    launcher = f"import subprocess; subprocess.run([{py!r}, '-c', {probe!r}])"
    code = (
        "import os, subprocess; print(os.getpid(), flush=True); "
        f"subprocess.run([{py!r}, '-c', {launcher!r}])"
    )
    # The symlink sits outside the venv, so hand the children our import path.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    run = subprocess.run(
        [str(claude), "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert run.returncode == 0, run.stderr
    out = run.stdout.split("\n")
    claude_pid, found = int(out[0]), json.loads(out[1])
    assert found["pid"] == claude_pid
    assert found["start_time"]


def test_no_claude_ancestor_is_none():
    # Not os.getpid(): the suite itself may run under a Claude Code session.
    assert find_claude(1) is None


def test_session_file_is_0600_and_keyed_by_the_claude_pid(tmp_path):
    cfg = Config(home=tmp_path / "h", port=1)
    path = write_session_file(cfg, {"pid": 4242, "start_time": "123"}, "web", "tok-1")
    assert path == tmp_path / "h" / "sessions" / "claude-4242.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "slug": "web",
        "agent_token": "tok-1",
        "claude_pid": 4242,
        "claude_start_time": "123",
    }


def test_read_session_returns_the_token_for_the_live_claude_process(tmp_path):
    from covey.mcp.identity import _start_time, read_session

    cfg = Config(home=tmp_path, port=1)
    claude = {"pid": os.getpid(), "start_time": _start_time(os.getpid())}
    write_session_file(cfg, claude, "api", "tok")
    env = {"CLAUDE_PID": str(os.getpid())}
    assert read_session(cfg, env) == {"slug": "api", "agent_token": "tok"}


def test_read_session_is_none_for_a_missing_file_a_recycled_pid_or_no_pid(tmp_path):
    from covey.mcp.identity import read_session

    cfg = Config(home=tmp_path, port=1)
    env = {"CLAUDE_PID": str(os.getpid())}
    assert read_session(cfg, env) is None  # no file
    write_session_file(cfg, {"pid": os.getpid(), "start_time": "stale"}, "api", "tok")
    assert read_session(cfg, env) is None  # start time differs
    assert read_session(cfg, {}) is None
    assert read_session(cfg, {"CLAUDE_PID": "not-a-pid"}) is None


CLAUDE = {"pid": 4242, "start_time": "777"}


def test_the_wake_file_is_0600_atomic_and_read_back_for_the_same_process(tmp_path):
    cfg = Config(home=tmp_path, port=1)
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o755)
    (sessions / "claude-4242.wake.tmp").write_text("left over")
    (sessions / "claude-4242.wake.tmp").chmod(0o644)
    write_session_file(cfg, CLAUDE, "web", "tok")
    path = write_wake_file(cfg, 4242, "777", "/run/claude.sock")
    assert path == wake_file(cfg, 4242) == sessions / "claude-4242.wake.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(sessions.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {
        "socket": "/run/claude.sock",
        "claude_pid": 4242,
        "claude_start_time": "777",
    }
    assert read_wake_file(cfg, CLAUDE) == "/run/claude.sock"
    assert sorted(p.name for p in sessions.iterdir()) == [
        "claude-4242.json",
        "claude-4242.wake.json",
    ]  # the token file is untouched and no tmp is left


def test_a_wake_file_from_a_recycled_pid_or_a_broken_file_reads_as_none(tmp_path):
    cfg = Config(home=tmp_path, port=1)
    assert read_wake_file(cfg, CLAUDE) is None  # no file
    write_wake_file(cfg, 4242, "old-start", "/run/claude.sock")
    assert read_wake_file(cfg, CLAUDE) is None  # another process's start time
    wake_file(cfg, 4242).write_text("{not json")
    assert read_wake_file(cfg, CLAUDE) is None
    wake_file(cfg, 4242).write_text(json.dumps({"claude_start_time": "777"}))
    assert read_wake_file(cfg, CLAUDE) is None  # no socket


def test_a_socket_path_the_api_would_reject_reads_as_none(tmp_path):
    """Sending it would get register a 400 and leave the session unregistered."""
    cfg = Config(home=tmp_path, port=1)
    write_wake_file(cfg, 4242, "777", "/run/" + "s" * 200 + ".sock")
    assert read_wake_file(cfg, CLAUDE) is None


def test_forgetting_a_wake_file_only_removes_the_one_for_this_process(tmp_path):
    cfg = Config(home=tmp_path, port=1)
    write_wake_file(cfg, 4242, "old-start", "/run/a.sock")
    forget_wake_file(cfg, CLAUDE)
    assert wake_file(cfg, 4242).exists()  # a recycled pid's file is not ours
    write_wake_file(cfg, 4242, "777", "/run/a.sock")
    forget_wake_file(cfg, CLAUDE)
    assert not wake_file(cfg, 4242).exists()
    forget_wake_file(cfg, CLAUDE)  # already gone: no error
