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
    # provider_config's `routing.json` target (#504 cut half) is isolated
    # automatically by conftest.py's autouse `_isolate_runtime`.
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


def test_switching_back_to_claude_wins_over_role_models_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-22 (dev cockpit, project agent-takkub): Lead had been pinned
    on codex through the model picker (`aliases-projects.json`), so picking
    "Lead ใช้ provider → Claude" wrote nothing — claude was treated as the
    implicit default and dropped from routing.json — and `provider_for` fell
    through to the role-models pin. Lead restarted on codex, twice."""
    from agent_takkub import role_models

    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Ok)
    role_models.set_model("lead", "codex", "gpt-5.6-sol", project="proj")
    assert provider_config.provider_for("lead", "proj") == "codex"
    window = _fake_window()

    UserActionsMixin._on_user_changed(window, "default", "claude")

    assert provider_config.provider_for("lead", "proj") == "claude"
    assert provider_config.effective_provider_for("lead", "proj") == "claude"
    assert user_profile.default_provider("proj") == "claude"
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


def test_selecting_codex_account_keeps_lead_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-09-17: picking an account in the 🤖 menu changes only that
    provider's account — it used to also flip Lead to that provider."""
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Ok)
    user_profile.add_profile("work", str(tmp_path / "codex-work"), provider="codex")
    window = _fake_window()
    codex_pane = MagicMock()
    codex_pane.model.provider_name = "codex"
    claude_pane = MagicMock()
    claude_pane.model.provider_name = "claude"
    window.orch._project_panes.return_value = {
        "lead": claude_pane,
        "codex": codex_pane,
        "frontend": claude_pane,
    }

    UserActionsMixin._on_account_selected(window, "work", "codex")

    assert user_profile.profile_for("proj", provider="codex") == "work"
    assert provider_config.provider_for("lead", "proj") == "claude"
    window._restart_lead_for_active_project.assert_not_called()
    window.orch.close.assert_called_once_with("codex", project="proj", reason="account_switch")


def test_codex_pane_env_uses_selected_account(tmp_path: Path) -> None:
    from agent_takkub import pane_env

    home = tmp_path / "codex-work"
    user_profile.add_profile("work", str(home), provider="codex")
    env: dict[str, str] = {}
    pane_env.inject_provider_home_env(env, "codex", "proj")
    assert "CODEX_HOME" not in env or env["CODEX_HOME"] != str(home)

    user_profile.set_profile("proj", "work", provider="codex")
    env = {}
    pane_env.inject_provider_home_env(env, "codex", "proj")
    assert env["CODEX_HOME"] == str(home)
    env = {}
    pane_env.inject_provider_home_env(env, "codex", "other-proj")
    assert env.get("CODEX_HOME") != str(home)
