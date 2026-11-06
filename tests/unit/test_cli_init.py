"""`covey init` against temp dirs only. HOME is redirected so no real settings are touched."""

import json

import pytest
from typer.testing import CliRunner

from covey.cli import app

runner = CliRunner()
EVENTS = {"SessionStart", "Stop", "PostToolUse", "SessionEnd"}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    return home, proj


@pytest.fixture
def hooks_ready(monkeypatch):
    monkeypatch.setattr("covey.cli.init.hooks_available", lambda: True)


def commands(settings, event):
    return [h["command"] for g in settings["hooks"][event] for h in g["hooks"]]


def test_project_init_writes_hooks_and_mcp(dirs, hooks_ready):
    _, proj = dirs
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == EVENTS
    assert commands(settings, "Stop") == ["covey hook stop"]
    assert commands(settings, "PostToolUse") == ["covey hook post-tool"]
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["covey"]["command"] == "covey"
    assert mcp["mcpServers"]["covey"]["args"] == ["mcp"]


def test_second_run_changes_nothing_and_says_so(dirs, hooks_ready):
    _, proj = dirs
    runner.invoke(app, ["init"])
    files = [proj / ".claude" / "settings.json", proj / ".mcp.json"]
    before = [p.read_text() for p in files]
    res = runner.invoke(app, ["init", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output)["changed"] is False
    assert [p.read_text() for p in files] == before
    assert "already" in runner.invoke(app, ["init"]).output.lower()


def test_preserves_unrelated_keys_and_existing_hooks(dirs, hooks_ready):
    _, proj = dirs
    (proj / ".claude").mkdir()
    existing = {"type": "command", "command": "echo mine"}
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [existing]}]}})
    )
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}})
    )
    assert runner.invoke(app, ["init"]).exit_code == 0
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert commands(settings, "Stop") == ["echo mine", "covey hook stop"]
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"other", "covey"}


def test_user_flag_targets_home_files(dirs, hooks_ready):
    home, proj = dirs
    res = runner.invoke(app, ["init", "--user"])
    assert res.exit_code == 0, res.output
    assert (home / ".claude" / "settings.json").exists()
    assert "covey" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert not (proj / ".mcp.json").exists()


def test_json_output_lists_changes(dirs, hooks_ready):
    payload = json.loads(runner.invoke(app, ["init", "--json"]).output)
    assert payload["changed"] is True
    assert len(payload["changes"]) == 5


def test_invalid_json_is_an_error_and_nothing_written(dirs):
    _, proj = dirs
    (proj / ".mcp.json").write_text("{not json")
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 1
    assert (proj / ".mcp.json").read_text() == "{not json"
    assert "fix" in res.output.lower()
    assert not (proj / ".claude" / "settings.json").exists()


def test_every_installed_hook_command_exists_in_the_cli(dirs):
    """A hook that exits 2 (usage error) would block Claude Code's Stop, so never write one."""
    _, proj = dirs
    assert runner.invoke(app, ["init"]).exit_code == 0
    settings_path = proj / ".claude" / "settings.json"
    if settings_path.exists():
        for event in json.loads(settings_path.read_text())["hooks"].values():
            for group in event:
                for h in group["hooks"]:
                    argv = h["command"].split()[1:]
                    res = runner.invoke(app, [*argv, "--help"])
                    assert res.exit_code == 0, h["command"]
    assert "covey" in json.loads((proj / ".mcp.json").read_text())["mcpServers"]
