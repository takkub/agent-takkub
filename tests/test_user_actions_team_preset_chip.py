"""#512 — the status-bar "ทีม: <preset> ▾" chip's click handlers.

Same `UserActionsMixin.<method>(fake_window, ...)` pattern as
test_user_actions_provider_switch.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QMenu, QWidget

from agent_takkub import team_preset
from agent_takkub import user_actions as actions_mod
from agent_takkub.user_actions import UserActionsMixin


@pytest.fixture(autouse=True)
def _isolate_team_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)


def _fake_window() -> MagicMock:
    window = MagicMock()
    window._chip_team_preset = MagicMock()
    return window


def _real_window() -> QWidget:
    """`_on_team_preset_chip_clicked` builds a real `QMenu(self)` — PyQt
    rejects a MagicMock as the parent, so the menu-construction tests need
    an actual (offscreen) QWidget standing in for MainWindow, with the
    orch/status attrs the handler touches stubbed on top."""
    window = QWidget()
    window.orch = MagicMock()
    window._status = MagicMock()
    window._chip_team_preset = MagicMock()
    return window


class TestTeamPresetMenuPick:
    def test_calls_orchestrator_set_team_preset(self) -> None:
        window = _fake_window()
        window.orch.set_team_preset.return_value = (True, "team preset set to full")

        UserActionsMixin._on_team_preset_menu_pick(window, "full", "proj-a")

        window.orch.set_team_preset.assert_called_once_with("full", "proj-a")
        window._status.showMessage.assert_not_called()

    def test_failure_shows_status_message(self) -> None:
        window = _fake_window()
        window.orch.set_team_preset.return_value = (False, "unknown team preset: nope")

        UserActionsMixin._on_team_preset_menu_pick(window, "nope", "proj-a")

        window._status.showMessage.assert_called_once()
        assert "unknown team preset" in window._status.showMessage.call_args[0][0]


class TestTeamPresetClearOverride:
    def test_calls_orchestrator(self) -> None:
        window = _fake_window()

        UserActionsMixin._on_team_preset_clear_override(window, "proj-a")

        window.orch.clear_team_preset_override.assert_called_once_with("proj-a")


class TestTeamPresetChipMenu:
    def test_no_active_project_shows_disabled_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(actions_mod, "active_project", lambda: (None, {}))
        monkeypatch.setattr(QMenu, "exec", lambda self, *a, **kw: None)
        window = _real_window()

        UserActionsMixin._on_team_preset_chip_clicked(window)  # must not raise
        window.orch.set_team_preset.assert_not_called()

    def test_builds_and_opens_without_error_for_active_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(actions_mod, "active_project", lambda: ("proj-a", {}))
        monkeypatch.setattr(QMenu, "exec", lambda self, *a, **kw: None)
        window = _real_window()

        UserActionsMixin._on_team_preset_chip_clicked(window)  # must not raise
        window.orch.set_team_preset.assert_not_called()  # nothing triggered — menu just opened

    def test_override_active_does_not_crash_menu_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        team_preset.set_current("full", "proj-a")
        team_preset.set_override("solo-lead", "proj-a")
        monkeypatch.setattr(actions_mod, "active_project", lambda: ("proj-a", {}))
        monkeypatch.setattr(QMenu, "exec", lambda self, *a, **kw: None)
        window = _real_window()

        UserActionsMixin._on_team_preset_chip_clicked(window)  # must not raise
