"""Accounts page login window lifetime (user 2026-09-17: "กด เข้าระบบแล้ว
ตัว cockpit เด้งหลุด แอปแคสเลย").

The login window used to be a child of the WA_DeleteOnClose Settings window,
so closing Settings freed the PtySession while its reader/writer QThreads were
still running — a Qt fatal error that aborted the whole cockpit.
"""

from __future__ import annotations

import threading

import pytest
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QWidget

from agent_takkub import pty_session, settings_accounts, terminal_widget


class _BusyThread(QThread):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.stop = threading.Event()

    def run(self) -> None:
        self.stop.wait(10)


class _FakeSession(QObject):
    bytesIn = pyqtSignal(bytes)
    processExited = pyqtSignal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.thread = _BusyThread(self)
        self.terminate_calls: list[bool] = []

    def spawn(self, argv, cwd=None, env=None) -> None:
        self.thread.start()

    def write(self, data) -> None:
        pass

    def resize(self, cols: int, rows: int) -> None:
        pass

    def terminate(self, wait: bool = False) -> None:
        self.terminate_calls.append(wait)
        self.thread.stop.set()


class _FakeTerm(QWidget):
    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)

    def write_bytes(self, data: bytes) -> None:
        pass


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pty_session, "PtySession", _FakeSession)
    monkeypatch.setattr(terminal_widget, "TerminalWidget", _FakeTerm)


def _open(parent: QWidget) -> settings_accounts._LoginPaneDialog:
    return settings_accounts._LoginPaneDialog(parent, "t", "", ["x"], {})


def test_login_window_outlives_the_settings_window() -> None:
    settings = QWidget()
    settings.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg = _open(settings)
    assert dlg.parent() is None
    assert dlg in settings_accounts._open_login_dialogs
    settings.close()  # deleting Settings must not free the running PTY threads
    assert dlg._session.thread.isRunning()
    dlg.close()


def test_close_stops_pty_threads_before_the_window_is_freed() -> None:
    dlg = _open(QWidget())
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    session = dlg._session
    dlg.close()
    assert session.terminate_calls == [True]
    assert session.thread.isFinished()


def test_failed_login_keeps_the_window_open_with_the_error() -> None:
    dlg = _open(QWidget())
    dlg.show()
    dlg._session.processExited.emit(1)
    assert dlg.isVisible()
    assert "code 1" in dlg._exit_lbl.text()
    dlg.close()


def test_quit_hook_closes_open_login_windows() -> None:
    dlg = _open(QWidget())
    session = dlg._session
    settings_accounts._close_login_dialogs_on_quit()
    assert session.terminate_calls == [True]
