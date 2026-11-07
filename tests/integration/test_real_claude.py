"""Opt-in: check every flag the worker argv uses against the installed `claude`.

Runs only with COVEY_TEST_REAL_CLAUDE=1. It reads `claude --help` and makes no model call.
Re-run it after every Claude Code upgrade; a renamed flag would otherwise only show up as
failed workers.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from covey.config import Config
from covey.daemon import waker

# Accepted by claude 2.1.319 but not listed in --help (tests/fixtures/worker/, runs 01-05).
HIDDEN = {"--max-turns"}

pytestmark = pytest.mark.skipif(
    os.environ.get("COVEY_TEST_REAL_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="set COVEY_TEST_REAL_CLAUDE=1 with claude on PATH",
)


def test_every_worker_flag_is_known_to_the_installed_claude(tmp_path):
    claude = shutil.which("claude")
    help_text = subprocess.run(
        [claude, "--help"], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    cfg = Config(home=tmp_path, port=7777, claude_bin=claude)
    argv = waker.build_argv(cfg, {}, Path("/x/mcp.json"), "frame")
    flags = {a for a in argv if a.startswith("-")}
    listed = set(re.findall(r"(?<![\w-])--?[A-Za-z][\w-]*", help_text))
    missing = sorted(flags - HIDDEN - listed)
    assert missing == []
