"""Structural smoke tests for the Phase 3 Plugins UI page (offscreen, no
pytest-qt needed — mirrors test_settings_management_ui_phase2.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import config, pane_tools_policy, plugin_installer
from agent_takkub.settings_management.commands import CreatePluginCommand
from agent_takkub.settings_management.pages.plugins_page import PluginsPage
from agent_takkub.settings_management.repositories import plugins as plugins_repo
from agent_takkub.settings_management.window import SettingsManagementWindow

_ENTRIES = [
    {
        "id": "github@claude-plugins-official",
        "version": "1.0.0",
        "scope": "user",
        "enabled": True,
        "installPath": "/cache/claude-plugins-official/github/1.0.0",
        "installedAt": "2026-07-01T00:00:00Z",
    },
    {
        "id": "security-guidance@claude-plugins-official",
        "version": "2.0.6",
        "scope": "user",
        "enabled": True,
        "installPath": "/cache/claude-plugins-official/security-guidance/2.0.6",
        "installedAt": "2026-07-01T00:00:00Z",
    },
]


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(config, "_SAFE_PLUGINS", ("claude-plugins-official",))
    entries = [dict(e) for e in _ENTRIES]
    monkeypatch.setattr(plugin_installer, "list_installed", lambda: [dict(e) for e in entries])
    monkeypatch.setattr(plugin_installer, "list_marketplaces", lambda: [])

    def _install(plugin_id: str) -> tuple[bool, str]:
        entries.append({"id": plugin_id, "version": "0.1.0", "scope": "user", "enabled": True})
        return True, "installed"

    def _uninstall(plugin_id: str) -> tuple[bool, str]:
        entries[:] = [e for e in entries if e["id"] != plugin_id]
        return True, "uninstalled"

    monkeypatch.setattr(plugin_installer, "install_by_id", _install)
    monkeypatch.setattr(plugin_installer, "uninstall_plugin", _uninstall)
    yield tmp_path


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_wires_plugins_into_sidebar() -> None:
    window = SettingsManagementWindow()
    assert window.sidebar.item(3).text() == "Plugins"
    window.sidebar.setCurrentRow(3)
    assert window.content_stack.currentWidget() is window.plugins_page


def test_plugins_page_lists_installed_plugins() -> None:
    page = PluginsPage()
    page.refresh()
    ids = {entity_id for entity_id, _ in page._load_rows()}
    assert ids == {"github@claude-plugins-official", "security-guidance@claude-plugins-official"}


def test_plugins_page_shows_blocked_banner_for_denylisted_plugin() -> None:
    page = PluginsPage()
    page.refresh()
    page.on_select("security-guidance@claude-plugins-official")
    # isVisible() is always False for an offscreen/never-shown widget tree;
    # isHidden() reflects the explicit setVisible() call this test cares about.
    assert page.blocked_banner.isHidden() is False
    assert "BLOCKED BY COCKPIT" in page.blocked_banner.text()
    assert page.roles_list.item(0).text() == "(assignment disabled — blocked by cockpit)"


def test_plugins_page_general_fields_are_never_editable_outside_create() -> None:
    page = PluginsPage()
    page.refresh()
    page.on_select("github@claude-plugins-official")
    assert page.key_edit.isEnabled() is False
    assert page.marketplace_combo.isEnabled() is False


def test_plugins_page_danger_zone_uses_uninstall_wording() -> None:
    page = PluginsPage()
    page.refresh()
    page.on_select("github@claude-plugins-official")
    assert page.danger_zone._delete_btn.text() == "Uninstall"


def test_plugins_page_new_button_and_footer_use_install_wording() -> None:
    page = PluginsPage()
    page.refresh()
    assert page.list._new_btn.text() == "+ Install Plugin"
    page.on_new()
    assert page.footer._save_btn.text() == "Install Plugin"


def test_plugins_page_create_flow_end_to_end() -> None:
    page = PluginsPage()
    page.refresh()
    page.on_new()
    page.key_edit.setText("frontend-design")
    page.marketplace_combo.setCurrentText("claude-plugins-official")
    ok = page._save()
    assert ok is True
    ids = {entity_id for entity_id, _ in page._load_rows()}
    assert "frontend-design@claude-plugins-official" in ids


def test_plugins_page_uninstall_flow_end_to_end() -> None:
    page = PluginsPage()
    page.refresh()
    page.on_select("github@claude-plugins-official")
    plan = plugins_repo.delete_plan("github@claude-plugins-official")
    page._on_delete_confirmed(plan.version)
    ids = {entity_id for entity_id, _ in page._load_rows()}
    assert "github@claude-plugins-official" not in ids


def test_plugins_page_manage_roles_button_switches_window_to_roles() -> None:
    window = SettingsManagementWindow()
    window.sidebar.setCurrentRow(3)
    window.plugins_page.manage_roles_requested()
    assert window.sidebar.currentRow() == 0
    assert window.content_stack.currentWidget() is window.roles_page


def test_create_plugin_command_defaults_marketplace_to_empty() -> None:
    assert CreatePluginCommand(key="foo").marketplace == ""
