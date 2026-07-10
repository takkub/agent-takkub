"""Widget smoke tests for settings_window.SettingsWindow (Phase 1).

Offscreen QPA (session-scoped QApplication from tests/conftest.py) —
"tofu" widget-property assertions per the task spec: stacked-page count, nav
switching, real create_role wiring, gold button style. Full interactive
visual verification is left to the user per the project's targeted-tests
rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog

from agent_takkub import (
    custom_roles,
    pipeline_config,
    provider_config,
    provider_state,
    settings_window,
)
from agent_takkub import roles as roles_mod


@pytest.fixture(autouse=True)
def _isolate_settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every on-disk store SettingsWindow touches to tmp, and clear
    the runtime custom-role registry so tests never leak into each other or
    the real ~/.takkub. provider_config's own paths are already isolated by
    the autouse fixture in tests/conftest.py."""
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    saved = dict(roles_mod._CUSTOM)
    roles_mod._CUSTOM.clear()
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


class TestSettingsWindowStructure:
    def test_has_seven_stacked_views(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.count() == 7
        dlg.deleteLater()

    def test_initial_view_defaults_to_providers_roles(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.currentIndex() == settings_window.VIEW_PROVIDERS_ROLES
        dlg.deleteLater()

    def test_nav_click_switches_stack_page(self) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._nav_buttons[settings_window.VIEW_MCP_MATRIX].click()
        assert dlg._stack.currentIndex() == settings_window.VIEW_MCP_MATRIX
        dlg.deleteLater()

    def test_active_nav_property_tracks_current_view(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        dlg._goto_view(settings_window.VIEW_MCP_MATRIX)
        assert dlg._nav_buttons[settings_window.VIEW_MCP_MATRIX].property("active") is True
        assert dlg._nav_buttons[settings_window.VIEW_PIPELINE_BUILDER].property("active") is False
        dlg.deleteLater()

    def test_save_button_uses_gold_style(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._save_btn.objectName() == "goldButton"
        dlg.deleteLater()

    def test_header_updates_with_view(self) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._goto_view(settings_window.VIEW_NEW_ROLE)
        assert dlg._content_title.text() == "New Role"
        dlg.deleteLater()


class TestNewRoleView:
    def test_create_role_persists_and_registers_live(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")
        dlg._nr_instructions.setPlainText("do data things")
        dlg._on_create_role_clicked()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert roles_mod.by_name("data-eng") is not None
        assert dlg._nr_status.text().startswith("✓")
        # Form resets on success (status message is deliberately kept).
        assert dlg._nr_name.text() == ""
        dlg.deleteLater()

    def test_reserved_name_rejected_without_creating(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("lead")
        dlg._on_create_role_clicked()

        assert "lead" not in custom_roles.load_custom_roles()
        assert dlg._nr_status.text().startswith("⚠️")
        dlg.deleteLater()


class TestProvidersRolesView:
    def test_save_apply_persists_role_enabled_and_provider(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()

        payload = pipeline_config.load(None)
        assert payload["rolesEnabled"]["qa"] is False
        assert provider_config.provider_for("backend") == "codex"
        assert dlg.result() == QDialog.DialogCode.Accepted
        dlg.deleteLater()

    def test_save_apply_stages_provider_disable_without_writing_disk(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._provider_toggles["codex"].setChecked(False)
        dlg._on_save_apply_clicked()

        assert dlg.pending_provider_disabled == {"codex": True}
        # Caller (user_actions._on_team_chip_clicked) applies this via
        # orchestrator.toggle_provider — SettingsWindow itself never writes
        # disabled-providers.json directly.
        assert provider_state.is_disabled("codex") is False
        dlg.deleteLater()

    def test_reset_reverts_unsaved_toggle(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        assert dlg._dirty is True
        dlg._on_reset_clicked()
        assert dlg._role_toggles["qa"].isChecked() is True
        assert dlg._dirty is False
        dlg.deleteLater()

    def test_lead_row_is_locked(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert "lead" not in dlg._role_toggles
        assert "lead" not in dlg._role_provider_combos
        dlg.deleteLater()
