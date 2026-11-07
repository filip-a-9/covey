"""notify.human: backend choice, never blocking, a failing backend reported once."""

import logging
import threading

import pytest

from covey.daemon import notify


@pytest.fixture(autouse=True)
def fresh_state():
    notify.reset()
    yield
    notify.reset()


class Recorder:
    """A backend that records what it was given and signals when it has."""

    def __init__(self):
        self.texts: list[str] = []
        self.got = threading.Event()

    def __call__(self, text):
        self.texts.append(text)
        self.got.set()


def osrelease(tmp_path, text):
    path = tmp_path / "osrelease"
    path.write_text(text)
    return path


def test_wsl_is_detected_case_insensitively(tmp_path):
    assert notify.is_wsl(osrelease(tmp_path, "6.1.0-Microsoft-standard-WSL2\n"))
    assert not notify.is_wsl(osrelease(tmp_path, "6.8.0-generic\n"))
    assert not notify.is_wsl(tmp_path / "missing")


def test_wsl_backend_passes_the_text_in_the_environment_not_the_script(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))

    backend = notify.select("auto", osrelease(tmp_path, "microsoft"), run=run)
    hostile = 'it\'s $(evil) `x` "q" ; Remove-Item *'
    backend(hostile)

    argv, kwargs = calls[0]
    assert argv[0] == "powershell.exe"
    assert all(hostile not in part for part in argv)
    assert kwargs["env"]["COVEY_TOAST_TEXT"] == hostile
    assert "COVEY_TOAST_TEXT" in kwargs["env"]["WSLENV"]
    assert kwargs["timeout"] > 0


def test_log_mode_has_no_backend_even_on_wsl(tmp_path):
    assert notify.select("log", osrelease(tmp_path, "microsoft")) is None


def test_off_wsl_without_plyer_there_is_no_backend(tmp_path):
    assert notify.select("auto", osrelease(tmp_path, "generic"), plyer=None) is None


def test_off_wsl_plyer_receives_the_text(tmp_path):
    seen = []

    class FakePlyer:
        @staticmethod
        def notify(**kw):
            seen.append(kw)

    backend = notify.select("auto", osrelease(tmp_path, "generic"), plyer=FakePlyer)
    backend("hello")
    assert seen[0]["message"] == "hello"


def test_human_logs_and_hands_the_text_to_the_backend(caplog):
    rec = Recorder()
    notify.use_backend(rec)
    with caplog.at_level(logging.WARNING, logger="covey.notify"):
        notify.human("request 01ABC needs you")
    assert rec.got.wait(5)
    assert rec.texts == ["request 01ABC needs you"]
    assert "01ABC" in caplog.text


def test_human_does_not_wait_for_a_slow_backend():
    release = threading.Event()
    notify.use_backend(lambda text: release.wait(10))
    done = threading.Event()
    threading.Thread(target=lambda: (notify.human("x"), done.set())).start()
    try:
        assert done.wait(2), "human() blocked on the backend"
    finally:
        release.set()


def test_a_failing_backend_is_reported_once_then_only_logged(caplog):
    calls = []

    def broken(text):
        calls.append(text)
        raise RuntimeError("no display")

    notify.use_backend(broken)
    with caplog.at_level(logging.WARNING, logger="covey.notify"):
        notify.human("first")
        notify.drain()
        notify.human("second")
        notify.drain()
    failures = [r for r in caplog.records if r.exc_info]
    assert len(failures) == 1
    assert "second" in caplog.text  # still logged
    assert calls == ["first"], "backend retried after failing"
