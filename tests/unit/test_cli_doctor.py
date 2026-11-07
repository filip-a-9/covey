"""The `claude for coveyd` and `notify backend` doctor checks, against a home with no daemon."""

import json
import os

import pytest
from typer.testing import CliRunner

from covey.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COVEY_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("COVEY_PORT", "7998")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def fake_exe(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def checks():
    res = runner.invoke(app, ["doctor", "--json"])
    return {c["name"]: c for c in json.loads(res.output)["checks"]}


def test_claude_for_coveyd_follows_covey_claude(tmp_path, monkeypatch):
    exe = fake_exe(tmp_path / "elsewhere", "my-claude")
    monkeypatch.setenv("COVEY_CLAUDE", str(exe))
    assert checks()["claude for coveyd"]["ok"] is True


def test_claude_for_coveyd_fails_with_a_fix_when_not_found(monkeypatch):
    monkeypatch.setenv("COVEY_CLAUDE", "/no/such/claude")
    c = checks()["claude for coveyd"]
    assert c["ok"] is False
    assert "COVEY_CLAUDE" in c["fix"]


def test_claude_for_coveyd_uses_path_when_covey_claude_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("COVEY_CLAUDE", raising=False)
    fake_exe(tmp_path / "bin", "claude")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert checks()["claude for coveyd"]["ok"] is True
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert checks()["claude for coveyd"]["ok"] is False


def test_notify_backend_warns_on_wsl_without_powershell(tmp_path, monkeypatch):
    monkeypatch.delenv("COVEY_NOTIFY", raising=False)
    monkeypatch.setattr("covey.daemon.notify.is_wsl", lambda *a, **k: True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    c = checks()["notify backend"]
    assert c["ok"] is False
    assert "COVEY_NOTIFY" in c["fix"] or "powershell" in c["fix"]


def test_notify_backend_ok_on_wsl_with_powershell(tmp_path, monkeypatch):
    monkeypatch.delenv("COVEY_NOTIFY", raising=False)
    monkeypatch.setattr("covey.daemon.notify.is_wsl", lambda *a, **k: True)
    fake_exe(tmp_path / "win", "powershell.exe")
    monkeypatch.setenv("PATH", str(tmp_path / "win") + os.pathsep + "/usr/bin")
    assert checks()["notify backend"]["ok"] is True


def test_notify_log_mode_needs_no_powershell(tmp_path, monkeypatch):
    monkeypatch.setenv("COVEY_NOTIFY", "log")
    monkeypatch.setattr("covey.daemon.notify.is_wsl", lambda *a, **k: True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert checks()["notify backend"]["ok"] is True
