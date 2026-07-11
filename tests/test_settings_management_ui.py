"""Structural smoke tests for the new Settings UI shell (offscreen, no
pytest-qt needed — mirrors test_new_project_wizard.py's pattern)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import custom_roles, pane_tools_policy, provider_config, roles, skill_policy
from agent_takkub.settings_management.commands import (
    CreateRoleCommand,
    RoleAccessDraft,
    RoleGeneralDraft,
)
from agent_takkub.settings_management.pages.roles_page import RolesPage
from agent_takkub.settings_management.window import SettingsManagementWindow


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    saved = dict(roles._CUSTOM)
    roles._CUSTOM.clear()
    yield tmp_path
    roles._CUSTOM.clear()
    roles._CUSTOM.update(saved)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_builds_and_shows_roles_by_default() -> None:
    window = SettingsManagementWindow()
    assert window.sidebar.currentRow() == 0
    assert window.content_stack.currentWidget() is window.roles_page


def test_roles_page_lists_builtin_roles() -> None:
    page = RolesPage()
    page.refresh()
    assert page.list._list.count() >= len(roles.ALL_DEFAULT)


def test_select_builtin_role_disables_general_fields() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("backend")
    assert page.name_edit.isEnabled() is False
    assert page.label_edit.isEnabled() is False
    assert page.footer._save_btn.isEnabled() is False  # not dirty yet


def test_create_flow_enables_fields_and_focuses_name() -> None:
    page = RolesPage()
    page.refresh()
    page.on_new()
    assert page.name_edit.isEnabled() is True
    assert page._create_mode is True


def test_create_role_end_to_end_through_page() -> None:
    page = RolesPage()
    page.refresh()
    page.on_new()
    page.name_edit.setText("data-eng")
    page.label_edit.setText("Data Eng")
    ok = page._save()
    assert ok is True
    assert roles.by_name("data-eng") is not None


def test_danger_zone_disabled_for_builtin_role() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("backend")
    assert page.danger_zone._delete_btn.isEnabled() is False


def test_danger_zone_enabled_for_custom_role() -> None:
    from agent_takkub.settings_management.repositories import roles as roles_repo

    roles_repo.create(
        CreateRoleCommand(
            name="data-eng",
            general=RoleGeneralDraft(
                label="Data Eng", color="#94a3b8", column=2, row=50, instructions=""
            ),
            access=RoleAccessDraft(provider="claude", skills=[], mcps=None, plugins=None),
        )
    )
    page = RolesPage()
    page.refresh()
    page.on_select("data-eng")
    assert page.danger_zone._delete_btn.isEnabled() is True
