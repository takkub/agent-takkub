"""closeEvent must tear down the remote-control bolt-on directly instead of
relying solely on QApplication.aboutToQuit — a live QSystemTrayIcon or other
top-level widget can keep the event loop alive past this window closing, in
which case aboutToQuit never fires and the remote HTTP server's daemon
thread keeps serving on a process that looks closed to the user."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

import agent_takkub.main_window as mw_mod


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_window_stub() -> mw_mod.MainWindow:
    with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
        win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

    win.orch = MagicMock()
    win.orch._panes_by_project = {}
    win.orch.write_session_snapshot = MagicMock()
    win.orch.write_resume_briefs = MagicMock()
    win._open_projects = MagicMock(return_value=["proj"])  # single tab: no confirm dialog
    win._save_window_state = MagicMock()
    win._persist_open_tabs = MagicMock()
    win._limit_store = None
    win.cli = MagicMock()
    return win


class TestCloseEventStopsRemote:
    def test_close_event_calls_remote_stop(self, qapp: QCoreApplication) -> None:
        win = _make_window_stub()
        win._remote = MagicMock()

        event = MagicMock()
        with patch("agent_takkub.main_window.QMainWindow.closeEvent") as super_close:
            mw_mod.MainWindow.closeEvent(win, event)

        win._remote.stop.assert_called_once()
        super_close.assert_called_once()

    def test_close_event_tolerates_no_remote(self, qapp: QCoreApplication) -> None:
        """enabled=false → self._remote is None; closeEvent must not blow up."""
        win = _make_window_stub()
        win._remote = None

        event = MagicMock()
        with patch("agent_takkub.main_window.QMainWindow.closeEvent") as super_close:
            mw_mod.MainWindow.closeEvent(win, event)

        super_close.assert_called_once()

    def test_close_event_swallows_remote_stop_exception(self, qapp: QCoreApplication) -> None:
        """A raising stop() must not prevent the rest of teardown from running."""
        win = _make_window_stub()
        win._remote = MagicMock()
        win._remote.stop.side_effect = RuntimeError("boom")

        event = MagicMock()
        with patch("agent_takkub.main_window.QMainWindow.closeEvent") as super_close:
            mw_mod.MainWindow.closeEvent(win, event)

        win._remote.stop.assert_called_once()
        win.cli.close.assert_called_once()
        super_close.assert_called_once()
