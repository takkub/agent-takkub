"""Regression tests for the status-bar provider/account switch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QMessageBox

from agent_takkub import provider_config, user_profile
from agent_takkub import user_actions as actions_mod
from agent_takkub.user_actions import UserActionsMixin


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(user_profile, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / "dot-claude")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(actions_mod, "active_project", lambda: ("proj", {}))


def _fake_window() -> MagicMock:
    window = MagicMock()
    window._limit_store = None
    return window


def test_switching_default_gemini_profile_updates_lead_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Ok)
    window = _fake_window()

    UserActionsMixin._on_user_changed(window, "default", "gemini")

    assert provider_config.provider_for("lead", "proj") == "gemini"
    assert user_profile.default_provider("proj") == "gemini"
    assert user_profile.profile_for("proj", provider="gemini") == "default"
    window._restart_lead_for_active_project.assert_called_once_with()


def test_cancel_keeps_existing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Cancel
    )
    window = _fake_window()

    UserActionsMixin._on_user_changed(window, "default", "gemini")

    assert provider_config.provider_for("lead", "proj") == "claude"
    assert user_profile.default_provider("proj") == "claude"
    window._restart_lead_for_active_project.assert_not_called()
