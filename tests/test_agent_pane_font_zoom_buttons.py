"""Header +/-/reset font-zoom buttons (2026-09-05 user request).

Exercises AgentPane._nudge_font_size / _reset_font_size and the header
button show/hide policy directly, with a fake TerminalWidget standing in
for the real QWebEngineView-backed one (same pattern as
test_agent_pane_auto_clear.py) and an in-memory QSettings stand-in (same
pattern as test_font_zoom.py) so the real "agent-takkub"/"cockpit"
settings on this machine are never touched.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import LEAD, by_name

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeSettings:
    """In-memory stand-in for QSettings("agent-takkub", "cockpit") — see
    test_font_zoom.py for why real QSettings must never be touched here."""

    _store: ClassVar[dict[str, object]] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def value(self, key):
        return self._store.get(key)

    def setValue(self, key, value):
        self._store[key] = value

    @classmethod
    def reset(cls) -> None:
        cls._store = {}


class _FakeTerminalWidget(QWidget):
    """Stand-in for TerminalWidget: mirrors the real clamp-and-emit
    behaviour of set_font_point_size() so AgentPane's _current_font_pt
    tracking (fed by the real fontSizeChanged signal) can be exercised
    without a real QWebEngineView."""

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)
    openInEditorRequested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.last_size: int | None = None
        self.call_count = 0

    def set_font_point_size(self, size: int) -> None:
        self.call_count += 1
        size = max(8, min(24, int(size)))
        self.last_size = size
        self.fontSizeChanged.emit(size)

    def clear_view(self) -> None:
        pass

    def set_keepalive(self, active: bool) -> None:
        pass

    def set_input_locked(self, locked: bool) -> None:
        pass

    def set_cwd(self, cwd) -> None:
        pass

    def write_bytes(self, data) -> None:
        pass

    def reset(self) -> None:
        pass

    def set_idle(self, idle: bool) -> None:
        pass

    def set_discard_enabled(self, enabled: bool) -> None:
        pass

    def set_discard_guard(self, guard) -> None:
        pass

    def setFocus(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    _FakeSettings.reset()
    monkeypatch.setattr(agent_pane_mod, "QSettings", _FakeSettings)


@pytest.fixture(autouse=True)
def _stop_done_clear_timer(monkeypatch):
    finalize = stop_timers_after(monkeypatch, AgentPane, "_done_clear_timer")
    yield
    finalize()


def _make_pane(role_name: str) -> AgentPane:
    role = LEAD if role_name == "lead" else by_name(role_name)
    pane = AgentPane(role)
    pane._idle_clear_timer.stop()
    return pane


class TestFontZoomButtons:
    def test_starts_at_default_pt(self, qapp):
        pane = _make_pane("qa")
        assert pane._current_font_pt == AgentPane._FONT_SIZE_DEFAULT_PT

    def test_zoom_in_increments_and_tracks_current(self, qapp):
        pane = _make_pane("qa")
        start = pane._current_font_pt
        pane._btn_zoom_in.click()
        assert pane._terminal.last_size == start + 1
        assert pane._current_font_pt == start + 1

    def test_zoom_out_decrements_and_tracks_current(self, qapp):
        pane = _make_pane("qa")
        start = pane._current_font_pt
        pane._btn_zoom_out.click()
        assert pane._terminal.last_size == start - 1
        assert pane._current_font_pt == start - 1

    def test_zoom_in_is_clamped_at_max(self, qapp):
        pane = _make_pane("qa")
        pane._current_font_pt = 24  # already at the widget's clamp ceiling
        pane._btn_zoom_in.click()
        assert pane._terminal.last_size == 24
        assert pane._current_font_pt == 24

    def test_zoom_out_is_clamped_at_min(self, qapp):
        pane = _make_pane("qa")
        pane._current_font_pt = 8  # already at the widget's clamp floor
        pane._btn_zoom_out.click()
        assert pane._terminal.last_size == 8
        assert pane._current_font_pt == 8

    def test_reset_button_restores_default(self, qapp):
        pane = _make_pane("qa")
        pane._nudge_font_size(5)
        assert pane._current_font_pt != AgentPane._FONT_SIZE_DEFAULT_PT
        pane._btn_zoom_reset.click()
        assert pane._terminal.last_size == AgentPane._FONT_SIZE_DEFAULT_PT
        assert pane._current_font_pt == AgentPane._FONT_SIZE_DEFAULT_PT

    def test_zoom_buttons_persist_via_qsettings(self, qapp):
        # Same fontSizeChanged -> _save_font_size path the pre-existing
        # wheel-zoom uses (test_font_zoom.py covers _save_font_size itself);
        # here we just confirm the button click reaches QSettings at all.
        pane = _make_pane("qa")
        pane._btn_zoom_in.click()
        assert _FakeSettings._store["pane/qa/font_pt"] == pane._current_font_pt
        assert _FakeSettings._store["pane/_default/font_pt"] == pane._current_font_pt

    def test_buttons_hidden_when_not_active(self, qapp):
        pane = _make_pane("qa")
        assert pane._btn_zoom_in.isHidden()
        assert pane._btn_zoom_out.isHidden()
        assert pane._btn_zoom_reset.isHidden()

    def test_buttons_shown_when_active_and_hidden_when_done(self, qapp):
        pane = _make_pane("qa")
        pane.set_state("active")
        assert not pane._btn_zoom_in.isHidden()
        assert not pane._btn_zoom_out.isHidden()
        assert not pane._btn_zoom_reset.isHidden()

        pane.set_state("done", note="finished")
        assert pane._btn_zoom_in.isHidden()
        assert pane._btn_zoom_out.isHidden()
        assert pane._btn_zoom_reset.isHidden()

    def test_lead_pane_also_gets_zoom_buttons(self, qapp):
        # Zoom lives at the pane-widget level, not gated to any one role or
        # provider — the Lead pane (user-driven, no lock button) still gets
        # font-zoom controls like every other pane.
        pane = _make_pane("lead")
        pane.set_state("active")
        assert not pane._btn_zoom_in.isHidden()
        assert not pane._btn_zoom_out.isHidden()
        assert not pane._btn_zoom_reset.isHidden()
