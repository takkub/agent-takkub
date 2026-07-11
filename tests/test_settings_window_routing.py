"""Tests for `UserActionsMixin._open_settings_window` — the feature-flag
routing choke point every Settings entry point (👥 Team chip, "Add / Remove
user…") goes through: TAKKUB_SETTINGS_UI=new lands the redesigned
SettingsManagementWindow for views it covers, everything else (Users tab and
any other legacy-only view) always opens the old SettingsWindow so no
feature goes missing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agent_takkub.user_actions as ua_mod
from agent_takkub.settings_window import VIEW_PROVIDERS_ROLES, VIEW_USERS


class _Stub(ua_mod.UserActionsMixin):
    def __init__(self) -> None:
        self._status = MagicMock()
        self.orch = MagicMock()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_SETTINGS_UI", raising=False)


class TestOpenSettingsWindowRouting:
    def test_new_flag_providers_roles_opens_new_window(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_SETTINGS_UI", "new")
        stub = _Stub()
        stub._open_settings_management_window = MagicMock()
        stub._open_legacy_settings_window = MagicMock()

        stub._open_settings_window(VIEW_PROVIDERS_ROLES)

        stub._open_settings_management_window.assert_called_once_with()
        stub._open_legacy_settings_window.assert_not_called()

    def test_new_flag_users_view_still_opens_legacy(self, monkeypatch):
        """VIEW_USERS has no redesigned equivalent yet — must not be dropped."""
        monkeypatch.setenv("TAKKUB_SETTINGS_UI", "new")
        stub = _Stub()
        stub._open_settings_management_window = MagicMock()
        stub._open_legacy_settings_window = MagicMock()

        stub._open_settings_window(VIEW_USERS)

        stub._open_legacy_settings_window.assert_called_once_with(VIEW_USERS)
        stub._open_settings_management_window.assert_not_called()

    def test_legacy_flag_opens_legacy_regardless_of_view(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_SETTINGS_UI", "legacy")
        stub = _Stub()
        stub._open_settings_management_window = MagicMock()
        stub._open_legacy_settings_window = MagicMock()

        stub._open_settings_window(VIEW_PROVIDERS_ROLES)

        stub._open_legacy_settings_window.assert_called_once_with(VIEW_PROVIDERS_ROLES)
        stub._open_settings_management_window.assert_not_called()

    def test_unset_env_defaults_to_legacy_window(self):
        # Default rolled back to LEGACY 2026-07-11 evening (user rejected the
        # new surface in real use) — new stays opt-in via TAKKUB_SETTINGS_UI.
        stub = _Stub()
        stub._open_settings_management_window = MagicMock()
        stub._open_legacy_settings_window = MagicMock()

        stub._open_settings_window(VIEW_PROVIDERS_ROLES)

        stub._open_legacy_settings_window.assert_called_once_with(VIEW_PROVIDERS_ROLES)
        stub._open_settings_management_window.assert_not_called()


class TestOpenSettingsManagementWindow:
    def test_creates_window_and_wires_legacy_hook(self, monkeypatch):
        stub = _Stub()
        stub._open_legacy_settings_window = MagicMock()

        stub._open_settings_management_window()

        win = stub._settings_management_window
        assert win is not None
        win.open_legacy_requested()
        stub._open_legacy_settings_window.assert_called_once_with(VIEW_PROVIDERS_ROLES)
        win.close()
