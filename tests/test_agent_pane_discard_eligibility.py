"""#364 lever 1 — AgentPane's discard-eligibility guard + on/off wiring.

`_discard_eligible` is the callable AgentPane installs on TerminalWidget via
`set_discard_guard`; TerminalWidget consults it only at the moment its own
debounce timer fires (see terminal_widget.py). Exercised directly here with a
fake TerminalWidget swapped in — a real QWebEngineView-backed one is flaky to
spawn under pytest (see test_terminal_widget.py) — same technique as
test_agent_pane_idle_flag.py / test_agent_pane_auto_clear.py.
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import (
    _DISCARD_STREAMING_GUARD_S,
    AgentPane,
    _env_pane_discard_override,
)
from agent_takkub.roles import LEAD, by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    """Records set_discard_enabled/set_discard_guard calls; no real
    QWebEngineView."""

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)
    openInEditorRequested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.discard_enabled_calls: list[bool] = []
        self.discard_guard = None

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

    def set_discard_enabled(self, enabled: bool) -> None:
        self.discard_enabled_calls.append(bool(enabled))

    def set_discard_guard(self, guard) -> None:
        self.discard_guard = guard


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


def _make_pane(role_name: str = "backend") -> AgentPane:
    pane = AgentPane(by_name(role_name) if role_name != "lead" else LEAD)
    pane._idle_clear_timer.stop()
    return pane


class TestDiscardGuardWiring:
    def test_construction_installs_guard_and_enabled_flag(self, qapp, monkeypatch):
        from agent_takkub import performance_settings

        # Pin the persisted settings read at construction instead of relying
        # on whatever (if anything) sits at performance_settings.path() on
        # the machine running this test.
        monkeypatch.setattr(
            agent_pane_mod.performance_settings,
            "load",
            lambda: performance_settings.preset("balanced"),
        )
        monkeypatch.delenv("TAKKUB_PANE_DISCARD", raising=False)
        pane = _make_pane()
        # Bound-method equality (not `is` — each attribute access mints a new
        # bound-method wrapper object even for the same underlying function).
        assert pane._terminal.discard_guard == pane._discard_eligible
        # "balanced" preset ships pane_discard_enabled=True (the dataclass default).
        assert pane._terminal.discard_enabled_calls == [True]


class TestDiscardEligibility:
    def test_lead_is_never_eligible(self, qapp):
        pane = _make_pane("lead")
        assert pane.role.name == LEAD.name
        assert pane._discard_eligible() is False

    def test_non_lead_with_no_output_history_is_eligible(self, qapp):
        pane = _make_pane("backend")
        assert pane._last_output_ts == 0.0
        assert pane._discard_eligible() is True

    def test_recent_output_vetoes_discard(self, qapp):
        pane = _make_pane("backend")
        pane._last_output_ts = time.time()
        assert pane._discard_eligible() is False

    def test_stale_output_is_eligible_again(self, qapp):
        pane = _make_pane("backend")
        pane._last_output_ts = time.time() - (_DISCARD_STREAMING_GUARD_S + 5)
        assert pane._discard_eligible() is True


class TestEnvOverride:
    def test_env_off_wins_over_persisted_on(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_PANE_DISCARD", "0")
        assert _env_pane_discard_override(True) is False

    def test_env_on_wins_over_persisted_off(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_PANE_DISCARD", "1")
        assert _env_pane_discard_override(False) is True

    def test_unset_env_falls_back_to_persisted(self, monkeypatch):
        monkeypatch.delenv("TAKKUB_PANE_DISCARD", raising=False)
        assert _env_pane_discard_override(True) is True
        assert _env_pane_discard_override(False) is False


class TestApplyPerformanceSettings:
    def test_live_reload_updates_discard_enabled(self, qapp, monkeypatch):
        import dataclasses

        from agent_takkub import performance_settings

        monkeypatch.delenv("TAKKUB_PANE_DISCARD", raising=False)
        pane = _make_pane()
        pane._terminal.discard_enabled_calls.clear()

        settings = dataclasses.replace(
            performance_settings.preset("balanced"), pane_discard_enabled=False
        )
        pane.apply_performance_settings(settings)

        assert pane._discard_enabled is False
        assert pane._terminal.discard_enabled_calls == [False]
