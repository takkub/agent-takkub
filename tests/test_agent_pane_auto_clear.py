"""Auto clear-view policy for teammate panes (done-report + idle timeout).

Exercises AgentPane's clear-view auto-trigger logic directly (calling the
timer slots instead of waiting on real QTimers) with a fake TerminalWidget
swapped in for the real QWebEngineView-backed one — real terminal widgets are
flaky to spawn in the offscreen test suite (see test_terminal_widget.py).
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import LEAD, by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    """Stand-in for TerminalWidget: records clear_view() calls, exposes the
    same signal/method surface AgentPane wires up, no real QWebEngineView."""

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.clear_view_calls = 0
        self.keepalive_calls: list[bool] = []
        self.input_locked = True
        self.cwd = None

    def clear_view(self) -> None:
        self.clear_view_calls += 1

    def set_keepalive(self, active: bool) -> None:
        self.keepalive_calls.append(bool(active))

    def set_input_locked(self, locked: bool) -> None:
        self.input_locked = bool(locked)

    def set_cwd(self, cwd) -> None:
        self.cwd = cwd

    def set_font_point_size(self, size: int) -> None:
        pass

    def write_bytes(self, data) -> None:
        pass

    def reset(self) -> None:
        pass

    def set_idle(self, idle: bool) -> None:
        pass

    def setFocus(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


def _make_pane(role_name: str) -> AgentPane:
    role = LEAD if role_name == "lead" else by_name(role_name)
    pane = AgentPane(role)
    # AgentPane.__init__ starts an always-on idle-clear QTimer; stop it so
    # tests control timing explicitly via _check_idle_auto_clear() calls.
    pane._idle_clear_timer.stop()
    return pane


class TestDoneAutoClear:
    def test_done_clears_after_delay_when_not_active_tab(self, qapp):
        pane = _make_pane("qa")
        pane._keepalive_active = False
        pane.set_state("done", note="finished")
        assert pane._done_clear_timer.isActive()
        pane._on_done_clear_timeout()  # simulate timer firing
        assert pane._terminal.clear_view_calls == 1
        assert pane._pending_auto_clear is False

    def test_done_defers_clear_while_active_tab(self, qapp):
        pane = _make_pane("qa")
        pane._keepalive_active = True  # user is looking at this pane
        pane.set_state("done", note="finished")
        pane._on_done_clear_timeout()
        assert pane._terminal.clear_view_calls == 0
        assert pane._pending_auto_clear is True

    def test_deferred_clear_flushes_when_tab_becomes_inactive(self, qapp):
        pane = _make_pane("qa")
        pane._keepalive_active = True
        pane.set_state("done", note="finished")
        pane._on_done_clear_timeout()
        assert pane._terminal.clear_view_calls == 0

        pane.set_keepalive(False)  # user navigates away
        assert pane._terminal.clear_view_calls == 1
        assert pane._pending_auto_clear is False

    def test_leaving_done_state_disarms_pending_clear(self, qapp):
        pane = _make_pane("qa")
        pane._keepalive_active = True
        pane.set_state("done", note="finished")
        pane._on_done_clear_timeout()
        assert pane._pending_auto_clear is True

        pane.set_state("active")  # respawned before the clear landed
        assert pane._pending_auto_clear is False
        pane.set_keepalive(False)
        assert pane._terminal.clear_view_calls == 0  # no stale clear fires

    def test_lead_never_auto_cleared(self, qapp):
        pane = _make_pane("lead")
        pane._keepalive_active = False
        pane.set_state("done", note="finished")
        # done-clear timer must never arm for Lead
        assert not pane._done_clear_timer.isActive()
        pane._on_done_clear_timeout()
        assert pane._terminal.clear_view_calls == 0


class TestIdleAutoClear:
    def test_idle_teammate_not_active_tab_clears(self, qapp):
        pane = _make_pane("backend")
        pane._keepalive_active = False
        pane.set_state("active")
        pane._last_output_ts = time.time() - 601  # just over 10 min
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 1
        assert pane._idle_auto_cleared is True

    def test_idle_below_threshold_does_not_clear(self, qapp):
        pane = _make_pane("backend")
        pane._keepalive_active = False
        pane.set_state("active")
        pane._last_output_ts = time.time() - 30  # well under 10 min
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 0

    def test_active_tab_never_idle_cleared(self, qapp):
        pane = _make_pane("backend")
        pane._keepalive_active = True  # user is on this pane
        pane.set_state("active")
        pane._last_output_ts = time.time() - 1_000
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 0

    def test_idle_clear_fires_once_until_new_output(self, qapp):
        pane = _make_pane("backend")
        pane._keepalive_active = False
        pane.set_state("active")
        pane._last_output_ts = time.time() - 1_000
        pane._check_idle_auto_clear()
        pane._check_idle_auto_clear()  # second poll tick — no repeat clear
        assert pane._terminal.clear_view_calls == 1

        pane._mark_output_ts(b"new bytes")  # fresh output resets the guard
        assert pane._idle_auto_cleared is False
        pane._last_output_ts = time.time() - 1_000
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 2

    def test_lead_never_idle_cleared(self, qapp):
        pane = _make_pane("lead")
        pane._keepalive_active = False
        pane.set_state("active")
        pane._last_output_ts = time.time() - 10_000
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 0

    def test_empty_state_not_idle_cleared(self, qapp):
        pane = _make_pane("backend")
        pane._keepalive_active = False
        # state defaults to "empty" — never spawned
        pane._last_output_ts = time.time() - 10_000
        pane._check_idle_auto_clear()
        assert pane._terminal.clear_view_calls == 0


class TestManualClearStillWorks:
    def test_manual_clear_button_calls_clear_view(self, qapp):
        pane = _make_pane("qa")
        pane._clear_pane_view()
        assert pane._terminal.clear_view_calls == 1
