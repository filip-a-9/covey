"""`covey init` against temp dirs only. HOME is redirected so no real settings are touched."""

import json
import shlex
import stat
import sys

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


@pytest.fixture(autouse=True)
def covey_exe(tmp_path, monkeypatch):
    """A real executable file named covey, standing in for the console script that launched us."""
    exe = tmp_path / "with space" / "bin" / "covey"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    return exe


@pytest.fixture
def hooks_ready(monkeypatch):
    monkeypatch.setattr("covey.cli.init.hooks_available", lambda: True)


def hook_argv(command):
    return shlex.split(command)


def commands(settings, event):
    return [h["command"] for g in settings["hooks"][event] for h in g["hooks"]]


def test_project_init_writes_hooks_and_mcp(dirs, hooks_ready, covey_exe):
    _, proj = dirs
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == EVENTS
    assert [hook_argv(c) for c in commands(settings, "Stop")] == [
        [str(covey_exe), "hook", "stop"]
    ]
    assert [hook_argv(c) for c in commands(settings, "PostToolUse")] == [
        [str(covey_exe), "hook", "post-tool"]
    ]
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["covey"]["command"] == str(covey_exe)
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


def test_preserves_unrelated_keys_and_existing_hooks(dirs, hooks_ready, covey_exe):
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
    stop = commands(settings, "Stop")
    assert stop[0] == "echo mine"
    assert hook_argv(stop[1]) == [str(covey_exe), "hook", "stop"]
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
                    argv = hook_argv(h["command"])[1:]
                    res = runner.invoke(app, [*argv, "--help"])
                    assert res.exit_code == 0, h["command"]
    assert "covey" in json.loads((proj / ".mcp.json").read_text())["mcpServers"]


def old_bare_settings():
    def group(cmd, matcher=None):
        g = {"hooks": [{"type": "command", "command": cmd}]}
        return {"matcher": matcher, **g} if matcher else g

    return {
        "hooks": {
            "SessionStart": [group("covey hook session-start")],
            "Stop": [group("echo mine"), group("covey hook stop")],
            "PostToolUse": [group("covey hook post-tool", "*")],
            "SessionEnd": [group("covey hook session-end")],
        }
    }


def test_init_replaces_old_bare_hooks_without_duplicating(dirs, hooks_ready, covey_exe):
    _, proj = dirs
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text(json.dumps(old_bare_settings()))
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"covey": {"command": "covey", "args": ["mcp"]}}})
    )
    assert runner.invoke(app, ["init"]).exit_code == 0
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    for event in EVENTS:
        argvs = [hook_argv(c) for c in commands(settings, event)]
        ours = [a for a in argvs if a and a[0].endswith("covey")]
        assert len(ours) == 1 and ours[0][0] == str(covey_exe)
    assert "echo mine" in commands(settings, "Stop")
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["covey"]["command"] == str(covey_exe)
    assert mcp["mcpServers"]["covey"]["args"] == ["mcp"]


def test_init_keeps_a_user_customised_mcp_command(dirs, hooks_ready):
    _, proj = dirs
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"covey": {"command": "/opt/mine/covey"}}})
    )
    runner.invoke(app, ["init"])
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["covey"]["command"] == "/opt/mine/covey"


def test_falls_back_to_covey_on_path_when_argv0_is_not_covey(
    dirs, hooks_ready, covey_exe, monkeypatch
):
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest"])
    monkeypatch.setenv("PATH", str(covey_exe.parent))
    _, proj = dirs
    assert runner.invoke(app, ["init"]).exit_code == 0
    mcp = json.loads((proj / ".mcp.json").read_text())
    assert mcp["mcpServers"]["covey"]["command"] == str(covey_exe)


def test_no_covey_executable_is_an_error_and_nothing_written(
    dirs, hooks_ready, monkeypatch, tmp_path
):
    empty_python = tmp_path / "py" / "python"
    empty_python.parent.mkdir()
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest"])
    monkeypatch.setattr(sys, "executable", str(empty_python))
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    _, proj = dirs
    res = runner.invoke(app, ["init", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.output)["error"]
    assert not (proj / ".mcp.json").exists()


@pytest.fixture
def ephemeral(tmp_path, monkeypatch):
    cache = tmp_path / "uvcache"
    exe = cache / "archive-v0" / "AbC123" / "bin" / "covey"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setattr(sys, "argv", [str(exe)])
    return exe


def test_uvx_environment_is_refused_with_the_next_step(dirs, hooks_ready, ephemeral):
    _, proj = dirs
    res = runner.invoke(app, ["init", "--json"])
    assert res.exit_code == 1
    assert "uv tool install covey" in json.loads(res.output)["error"]
    assert not (proj / ".mcp.json").exists()
    assert not (proj / ".claude" / "settings.json").exists()
    assert runner.invoke(app, ["init"]).exit_code == 1


@pytest.mark.parametrize(
    ("args", "next_step"),
    [(["init"], "&& covey init`"), (["init", "--user"], "&& covey init --user`")],
)
def test_uvx_refusal_names_the_scope_the_user_ran(
    dirs, hooks_ready, ephemeral, args, next_step
):
    res = runner.invoke(app, [*args, "--json"])
    assert res.exit_code == 1
    assert next_step in json.loads(res.output)["error"]


def test_uv_default_cache_layout_is_recognised_without_the_env_var(
    dirs, hooks_ready, tmp_path, monkeypatch
):
    exe = tmp_path / "x" / ".cache" / "uv" / "archive-v0" / "H4sh" / "bin" / "covey"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    assert runner.invoke(app, ["init"]).exit_code == 1


def test_a_uv_tool_install_is_not_ephemeral(dirs, hooks_ready, tmp_path, monkeypatch):
    exe = tmp_path / "share" / "uv" / "tools" / "covey-mesh" / "bin" / "covey"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    assert runner.invoke(app, ["init"]).exit_code == 0


def test_write_json_keeps_the_files_mode_and_ignores_a_planted_tmp_symlink(tmp_path):
    from covey.cli.init import write_json

    target = tmp_path / ".claude.json"
    target.write_text("{}")
    target.chmod(0o600)
    write_json(target, {"a": 1})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    victim = tmp_path / "victim.txt"
    victim.write_text("orig")
    (tmp_path / ".claude.json.covey-tmp").symlink_to(victim)
    write_json(target, {"b": 2})
    assert victim.read_text() == "orig"
    assert json.loads(target.read_text()) == {"b": 2}
