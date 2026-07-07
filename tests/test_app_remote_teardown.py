"""H-E: `_install_signal_handlers`'s `_kill_all` closure must stop the
remote-control bolt-on on every teardown path it already covers (atexit,
`aboutToQuit`, SIGINT/SIGTERM/SIGBREAK) — not just `RemoteControl`'s own
`aboutToQuit` connection, which a crash or hard kill never reaches.
"""

from __future__ import annotations

import atexit
from unittest.mock import MagicMock

import agent_takkub.app as app_mod


def _install_and_capture_kill_all(monkeypatch, window) -> callable:
    """Isolates the atexit-registration path: no real signal handlers get
    installed and no real QApplication is touched, but the exact same
    `_kill_all` closure `_install_signal_handlers` builds is captured."""
    captured = {}
    monkeypatch.setattr(atexit, "register", lambda fn: captured.setdefault("kill_all", fn))
    monkeypatch.setattr(app_mod.signal, "signal", lambda *a, **kw: None)
    monkeypatch.setattr(app_mod.QApplication, "instance", staticmethod(lambda: None))
    app_mod._install_signal_handlers(window)
    return captured["kill_all"]


class _FakeWindow:
    def __init__(self) -> None:
        self.orch = MagicMock()
        self.orch._panes_by_project = {}
        self.cli = MagicMock()
        self._remote = MagicMock()


def test_kill_all_stops_remote_control(monkeypatch):
    win = _FakeWindow()
    kill_all = _install_and_capture_kill_all(monkeypatch, win)
    kill_all()
    win._remote.stop.assert_called_once()


def test_kill_all_tolerates_remote_none(monkeypatch):
    win = _FakeWindow()
    win._remote = None
    kill_all = _install_and_capture_kill_all(monkeypatch, win)
    kill_all()  # must not raise


def test_kill_all_tolerates_missing_remote_attr(monkeypatch):
    win = _FakeWindow()
    del win._remote
    kill_all = _install_and_capture_kill_all(monkeypatch, win)
    kill_all()  # must not raise


def test_kill_all_tolerates_remote_stop_raising(monkeypatch):
    win = _FakeWindow()
    win._remote.stop.side_effect = RuntimeError("boom")
    kill_all = _install_and_capture_kill_all(monkeypatch, win)
    kill_all()  # must not raise — a broken remote teardown can't block pane teardown
