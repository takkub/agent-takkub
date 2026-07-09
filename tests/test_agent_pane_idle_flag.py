"""#106: AgentPane._sync_idle_flag must poll the lock-free cached ready state
(PtySession.is_at_ready_prompt_cached()), not the lock-guarded
is_at_ready_prompt() — the latter contends PtySession._screen_lock with the
reader thread's stream.feed() on every outputUpdated, the main-thread jank
root cause. See docs/plans/2026-07-09-jank-findings.md.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    """Minimal stand-in — only the surface _sync_idle_flag touches matters here."""

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)

    def set_idle(self, idle: bool) -> None:
        pass

    def set_keepalive(self, active: bool) -> None:
        pass

    def set_input_locked(self, locked: bool) -> None:
        pass

    def set_cwd(self, cwd) -> None:
        pass

    def set_font_point_size(self, size: int) -> None:
        pass

    def write_bytes(self, data) -> None:
        pass

    def reset(self) -> None:
        pass

    def setFocus(self) -> None:
        pass

    def clear_view(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


def _make_pane(qapp) -> AgentPane:
    pane = AgentPane(by_name("backend"))
    pane._idle_clear_timer.stop()
    return pane


def test_sync_idle_flag_uses_cached_accessor_not_locked_one(qapp) -> None:
    """The main-thread poll must call is_at_ready_prompt_cached(), never
    is_at_ready_prompt() — calling the locked one here is the exact #106
    regression (main thread taking _screen_lock, contending the reader
    thread)."""
    pane = _make_pane(qapp)
    sess = MagicMock()
    sess.is_at_ready_prompt_cached.return_value = True
    sess.is_at_ready_prompt.side_effect = AssertionError(
        "_sync_idle_flag must not call the lock-guarded is_at_ready_prompt()"
    )
    pane.session = sess
    pane._idle_check_at = 0.0

    pane._sync_idle_flag()

    sess.is_at_ready_prompt_cached.assert_called_once()
    sess.is_at_ready_prompt.assert_not_called()
    assert pane._last_idle is True


def test_sync_idle_flag_throttles_within_poll_window(qapp) -> None:
    pane = _make_pane(qapp)
    sess = MagicMock()
    sess.is_at_ready_prompt_cached.return_value = True
    pane.session = sess
    pane._idle_check_at = time.time()  # just polled

    pane._sync_idle_flag()

    sess.is_at_ready_prompt_cached.assert_not_called()


def test_sync_idle_flag_exception_treated_as_busy(qapp) -> None:
    pane = _make_pane(qapp)
    sess = MagicMock()
    sess.is_at_ready_prompt_cached.side_effect = RuntimeError("boom")
    pane.session = sess
    pane._idle_check_at = 0.0
    pane._last_idle = True

    pane._sync_idle_flag()  # must not raise

    assert pane._last_idle is False
