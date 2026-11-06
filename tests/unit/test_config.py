from pathlib import Path

import pytest

from covey.config import ConfigError, load


def test_defaults():
    cfg = load({})
    assert cfg.home == Path.home() / ".covey"
    assert cfg.port == 7777


def test_env_overrides(tmp_path):
    cfg = load({"COVEY_HOME": str(tmp_path), "COVEY_PORT": "8123"})
    assert cfg.socket_path == tmp_path / "covey.sock"
    assert cfg.port == 8123


@pytest.mark.parametrize("raw", ["abc", "0", "70000"])
def test_bad_port_is_refused(raw):
    with pytest.raises(ConfigError):
        load({"COVEY_PORT": raw})
