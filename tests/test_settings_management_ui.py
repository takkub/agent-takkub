"""Structural smoke tests for the new Settings UI shell (offscreen, no
pytest-qt needed — mirrors test_new_project_wizard.py's pattern)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import (
    custom_roles,
    pane_tools_policy,
    provider_config,
    roles,
    shared_dev_tools,
    skill_policy,
)
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
    # Access-tab MCP writes now regen role variants (HIGH-4) — redirect the
    # master file so that never touches the real ~/.takkub runtime dir.
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
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


def test_danger_zone_hidden_for_builtin_role() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("backend")
    assert page.danger_zone.isHidden() is True


def test_danger_zone_visible_for_custom_role() -> None:
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
    assert page.danger_zone.isHidden() is False


def test_lead_provider_note_empty_when_claude() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("lead")
    assert page.provider_combo.currentText() == "claude"
    assert page.provider_note.text() == ""


def test_lead_provider_note_warns_reactively_on_non_claude_selection() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("lead")
    idx = page.provider_combo.findText("codex")
    page.provider_combo.setCurrentIndex(idx)
    assert "mirror" in page.provider_note.text()
    # Switching back to claude clears the warning live (reactive, not static).
    idx_claude = page.provider_combo.findText("claude")
    page.provider_combo.setCurrentIndex(idx_claude)
    assert page.provider_note.text() == ""


def test_non_lead_role_never_shows_lead_capability_note() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("backend")
    idx = page.provider_combo.findText("codex")
    page.provider_combo.setCurrentIndex(idx)
    assert page.provider_note.text() == ""


def test_search_filters_role_list() -> None:
    page = RolesPage()
    page.refresh()
    total = page.list._list.count()
    page.list._search.setText("backend")
    assert page.list._list.count() < total
    names = [page.list._list.item(i).text() for i in range(page.list._list.count())]
    assert all("backend" in n.lower() for n in names)
    page.list._search.setText("")
    assert page.list._list.count() == total


def test_filter_chip_filters_role_list_to_builtin_only() -> None:
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
    page._on_filter_changed("Built-in")
    names = {page.list._list.item(i).text() for i in range(page.list._list.count())}
    assert all("built-in" in n.lower() for n in names)
    assert not any("data eng" in n.lower() for n in names)
    page._on_filter_changed("All")
    assert page.list._list.count() >= len(roles.ALL_DEFAULT) + 1


def test_settings_window_has_object_name_for_theming() -> None:
    window = SettingsManagementWindow()
    assert window.objectName() == "settingsWindow"


# ── draft-guard coverage (codex cross-check MEDIUM-2) ──────────────────────
# window.py's sidebar nav / "Open legacy settings" / closeEvent all bypassed
# ManagementPage's Save/Discard/Keep-editing guard before this fix — a
# dirty draft on the current page could be silently discarded by any of the
# three. confirm_navigate_away() is the shared choke point; the tests below
# cover (1) the page-level dialog wiring and (2) window.py's three call sites
# via a monkeypatched confirm_navigate_away (no real dialogs in headless CI).


def _make_backend_dirty(page: RolesPage) -> None:
    page.on_select("backend")
    page.label_edit.setText("Backend Renamed")
    assert page._dirty is True


def test_confirm_navigate_away_true_when_not_dirty() -> None:
    page = RolesPage()
    page.refresh()
    page.on_select("backend")
    assert page.confirm_navigate_away() is True


def test_confirm_navigate_away_discard_clears_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    page = RolesPage()
    page.refresh()
    _make_backend_dirty(page)
    monkeypatch.setattr(page, "_ask_draft_guard", lambda: QMessageBox.StandardButton.Discard)

    assert page.confirm_navigate_away() is True
    assert page._dirty is False


def test_confirm_navigate_away_cancel_keeps_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    page = RolesPage()
    page.refresh()
    _make_backend_dirty(page)
    monkeypatch.setattr(page, "_ask_draft_guard", lambda: QMessageBox.StandardButton.Cancel)

    assert page.confirm_navigate_away() is False
    assert page._dirty is True


def test_confirm_navigate_away_save_persists_and_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtWidgets import QMessageBox

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
    page.label_edit.setText("Data Eng Renamed")
    monkeypatch.setattr(page, "_ask_draft_guard", lambda: QMessageBox.StandardButton.Save)

    assert page.confirm_navigate_away() is True
    assert roles.by_name("data-eng").label == "Data Eng Renamed"


def test_sidebar_nav_reverted_when_current_page_blocks_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = SettingsManagementWindow()
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: False)

    window.sidebar.setCurrentRow(1)

    assert window.sidebar.currentRow() == 0
    assert window.content_stack.currentWidget() is window.roles_page


def test_sidebar_nav_proceeds_when_current_page_allows_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = SettingsManagementWindow()
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: True)

    window.sidebar.setCurrentRow(1)

    assert window.sidebar.currentRow() == 1
    assert window.content_stack.currentWidget() is window.skills_page


def test_open_legacy_button_blocked_when_current_page_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = SettingsManagementWindow()
    calls: list[bool] = []
    window.open_legacy_requested = lambda: calls.append(True)
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: False)

    window._on_open_legacy_clicked()

    assert calls == []


def test_open_legacy_button_fires_when_current_page_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = SettingsManagementWindow()
    calls: list[bool] = []
    window.open_legacy_requested = lambda: calls.append(True)
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: True)

    window._on_open_legacy_clicked()

    assert calls == [True]


def test_close_event_ignored_when_current_page_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtGui import QCloseEvent

    window = SettingsManagementWindow()
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: False)
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted() is False


def test_close_event_accepted_when_current_page_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtGui import QCloseEvent

    window = SettingsManagementWindow()
    monkeypatch.setattr(window.roles_page, "confirm_navigate_away", lambda: True)
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted() is True
