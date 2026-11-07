"""Tell the human something: always log it, and pop a desktop notification if one is possible.

Backends, chosen once by `select`: a Windows toast through powershell.exe on WSL, plyer
elsewhere if it imports, otherwise log only. The backend runs on a worker thread so the
event loop never waits for it. A backend that fails is reported once and then dropped.
"""

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("covey.notify")

OSRELEASE = Path("/proc/sys/kernel/osrelease")
TOAST_TIMEOUT_S = 15.0
Backend = Callable[[str], None]

# The text arrives in $env:COVEY_TOAST_TEXT and is only ever used as a string value.
# The AppId is Windows PowerShell's, registered on stock Windows 10/11.
TOAST_SCRIPT = r"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02')
$text = $xml.GetElementsByTagName('text')
$null = $text.Item(0).AppendChild($xml.CreateTextNode('Covey'))
$null = $text.Item(1).AppendChild($xml.CreateTextNode($env:COVEY_TOAST_TEXT))
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""

_lock = threading.Lock()
_mode = "auto"
_backend: Backend | None = None
_resolved = False
_failed = False
_threads: list[threading.Thread] = []


def is_wsl(osrelease: Path = OSRELEASE) -> bool:
    try:
        return "microsoft" in osrelease.read_text().lower()
    except OSError:
        return False


def _toast(run: Callable[..., Any]) -> Backend:
    def send(text: str) -> None:
        env = {**os.environ, "COVEY_TOAST_TEXT": text}
        env["WSLENV"] = ":".join(filter(None, [env.get("WSLENV"), "COVEY_TOAST_TEXT"]))
        run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                TOAST_SCRIPT,
            ],
            env=env,
            timeout=TOAST_TIMEOUT_S,
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

    return send


def _import_plyer() -> Any:
    try:
        from plyer import notification
    except Exception:  # noqa: BLE001 (not installed, or no platform support)
        return None
    return notification


def backend_name(mode: str, osrelease: Path = OSRELEASE, plyer: Any = ...) -> str:
    """Which backend `select` picks: "log", "powershell" or "plyer"."""
    if mode == "log":
        return "log"
    if is_wsl(osrelease):
        return "powershell"
    module = _import_plyer() if plyer is ... else plyer
    return "log" if module is None else "plyer"


def select(
    mode: str,
    osrelease: Path = OSRELEASE,
    run: Callable[..., Any] = subprocess.run,
    plyer: Any = ...,
) -> Backend | None:
    """The backend for `mode`, or None to only log. `plyer=None` means it is not installed."""
    if plyer is ...:
        plyer = _import_plyer()
    name = backend_name(mode, osrelease, plyer)
    if name == "powershell":
        return _toast(run)
    if name == "plyer":
        return lambda text: plyer.notify(title="Covey", message=text, timeout=10)
    return None


def configure(mode: str) -> None:
    global _mode, _backend, _resolved, _failed
    with _lock:
        _mode, _backend, _resolved, _failed = mode, None, False, False


def use_backend(backend: Backend | None) -> None:
    """Replace the backend (tests inject a recorder here)."""
    global _backend, _resolved, _failed
    with _lock:
        _backend, _resolved, _failed = backend, True, False


def reset() -> None:
    configure("auto")


def drain() -> None:
    """Wait for notifications already handed to a backend."""
    for thread in _threads:
        thread.join(TOAST_TIMEOUT_S + 5)
    _threads[:] = [t for t in _threads if t.is_alive()]


def _deliver(backend: Backend, text: str) -> None:
    global _failed
    try:
        backend(text)
    except Exception:
        with _lock:
            first, _failed = not _failed, True
        if first:
            logger.warning(
                "Desktop notifications failed, logging only from now on."
                " Set COVEY_NOTIFY=log to silence this.",
                exc_info=True,
            )


def human(text: str) -> None:
    """Log `text` and, if a backend works, show it on the desktop. Never blocks."""
    global _backend, _resolved
    logger.warning("%s", text)
    with _lock:
        if not _resolved:
            _backend, _resolved = select(_mode), True
        backend = None if _failed else _backend
    if backend is None:
        return
    thread = threading.Thread(target=_deliver, args=(backend, text), daemon=True)
    _threads.append(thread)
    thread.start()
