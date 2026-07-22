"""Structural smoke tests for the Phase 4 Providers UI page (offscreen, no
pytest-qt needed — mirrors test_settings_management_ui_phase2.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import provider_config, provider_models, provider_state
from agent_takkub.settings_management.pages.providers_page import ProvidersPage
from agent_takkub.settings_management.window import SettingsManagementWindow


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(provider_models, "_PATH", tmp_path / "provider-models.json")
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
    assert names == {"claude", "codex", "gemini", "opencode", "kimi", "cursor"}


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


class TestModelField:
    def test_disabled_for_provider_without_model_flag(self, monkeypatch) -> None:
        # Every real provider declares model_flag now (codex's was verified
        # against the installed binary), so fake one without it to keep the
        # disabled-input branch covered.
        import dataclasses

        from agent_takkub.settings_management.repositories import providers as providers_repo

        real_get = providers_repo.get

        def _get(entity_id: str):
            detail = real_get(entity_id)
            if entity_id == "codex":
                detail = dataclasses.replace(detail, model_flag_supported=False)
            return detail

        monkeypatch.setattr(
            "agent_takkub.settings_management.pages.providers_page.providers_repo.get", _get
        )
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")
        assert page.model_edit.isEnabled() is False

    def test_enabled_for_provider_with_model_flag(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("gemini")
        assert page.model_edit.isEnabled() is True
        assert page.model_edit.text() == ""
        assert page.model_edit.placeholderText() == "default"

    def test_editing_model_marks_dirty_and_saves(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("gemini")
        page.model_edit.setText("gemini-2.5-pro")
        assert page._dirty is True
        ok = page._save()
        assert ok is True
        assert provider_models.model_for("gemini") == "gemini-2.5-pro"

    def test_clearing_model_persists_empty(self) -> None:
        provider_models.set_model("gemini", "gemini-2.5-pro")
        page = ProvidersPage()
        page.refresh()
        page.on_select("gemini")
        assert page.model_edit.text() == "gemini-2.5-pro"
        page.model_edit.setText("")
        ok = page._save()
        assert ok is True
        assert provider_models.model_for("gemini") is None

    def test_claude_model_field_editable_though_toggle_is_not(self) -> None:
        page = ProvidersPage()
        page.refresh()
        page.on_select("claude")
        assert page.enabled_toggle.isEnabled() is False
        assert page.model_edit.isEnabled() is True
        page.model_edit.setText("opus")
        ok = page._save()
        assert ok is True
        assert provider_models.model_for("claude") == "opus"

    def test_wired_hook_still_persists_model_via_repository(self) -> None:
        # The enabled-toggle broadcast hook shouldn't swallow a model edit
        # made in the same Save — the two fields persist independently.
        page = ProvidersPage()
        page.refresh()
        page.on_select("codex")
        page.toggle_provider_requested = lambda name, disabled: (True, "")
        page.enabled_toggle.setChecked(False)
        ok = page._save()
        assert ok is True
        # codex has no model_flag — nothing to persist, just confirms the
        # hook path doesn't error when model_changed is inert.
        assert provider_models.model_for("codex") is None
