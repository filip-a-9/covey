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
    write_session_file,
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
