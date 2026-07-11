"""Structural smoke tests for the Phase 4 Providers UI page (offscreen, no
pytest-qt needed — mirrors test_settings_management_ui_phase2.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import provider_config, provider_state
from agent_takkub.settings_management.pages.providers_page import ProvidersPage
from agent_takkub.settings_management.window import SettingsManagementWindow


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    yield tmp_path


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_wires_providers_into_sidebar() -> None:
    window = SettingsManagementWindow()
    assert window.sidebar.item(4).text() == "Providers"
    window.sidebar.setCurrentRow(4)
    assert window.content_stack.currentWidget() is window.providers_page


def test_providers_page_new_button_is_hidden() -> None:
    page = ProvidersPage()
    assert page.list._new_btn.isVisible() is False


def test_providers_page_lists_all_providers() -> None:
    page = ProvidersPage()
    page.refresh()
    names = {name for name, _ in page._load_rows()}
    assert names == {"claude", "codex", "gemini"}


def test_providers_page_claude_toggle_is_disabled() -> None:
    page = ProvidersPage()
    page.refresh()
    page.on_select("claude")
    assert page.enabled_toggle.isEnabled() is False
    assert page.enabled_toggle.isChecked() is True


def test_providers_page_codex_toggle_editable_and_saves() -> None:
    page = ProvidersPage()
    page.refresh()
    page.on_select("codex")
    assert page.enabled_toggle.isEnabled() is True
    page.enabled_toggle.setChecked(False)
    assert page._dirty is True
    ok = page._save()
    assert ok is True
    assert provider_state.is_disabled("codex") is True


def test_providers_page_manage_roles_button_switches_window_to_roles() -> None:
    window = SettingsManagementWindow()
    window.sidebar.setCurrentRow(4)
    window.providers_page.manage_roles_requested()
    assert window.sidebar.currentRow() == 0
    assert window.content_stack.currentWidget() is window.roles_page


class TestToggleProviderHook:
    """MED-1: an enabled/disabled Save must route through the shell's
    broadcast-capable hook when one is wired, instead of silently falling
    back to repository-only persistence (which never notifies live Lead
    panes)."""

    def test_unset_hook_falls_back_to_repository_persistence(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")
        page.enabled_toggle.setChecked(False)
        ok = page._save()
        assert ok is True
        assert provider_state.is_disabled("codex") is True

    def test_wired_hook_is_called_instead_of_repository_and_wins(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")
        page.enabled_toggle.setChecked(False)

        calls: list[tuple[str, bool]] = []

        def hook(name: str, disabled: bool) -> tuple[bool, str]:
            calls.append((name, disabled))
            return True, ""

        page.toggle_provider_requested = hook
        ok = page._save()
        assert ok is True
        assert calls == [("codex", True)]
        # The hook is the ONLY thing that ran — repository-only persistence
        # (which never broadcasts) never touched provider_state.
        assert provider_state.is_disabled("codex") is False

    def test_wired_hook_failure_shows_error_and_does_not_clear_dirty(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")
        page.enabled_toggle.setChecked(False)
        page.toggle_provider_requested = lambda name, disabled: (False, "broadcast failed")

        from unittest.mock import patch

        with patch.object(page, "_show_error") as show_error:
            ok = page._save()
        assert ok is False
        show_error.assert_called_once_with("broadcast failed")
        assert page._dirty is True

    def test_unchanged_toggle_never_calls_hook(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")  # no toggle change — Save with nothing dirty

        calls: list[tuple[str, bool]] = []
        page.toggle_provider_requested = lambda name, disabled: calls.append((name, disabled))
        ok = page._save()
        assert ok is True
        assert calls == []
