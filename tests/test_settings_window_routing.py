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


class _FakeAcceptedDialog:
    """Stand-in for `SettingsWindow` — accepted with no staged provider/perf
    change, so `_open_legacy_settings_window`'s only observable side effect
    to assert on is the #510 rolesEnabled diff+broadcast."""

    from PyQt6.QtWidgets import QDialog

    DialogCode = QDialog.DialogCode

    def __init__(self, parent, project=None, initial_view=None) -> None:
        self.project = project
        self.pending_provider_disabled: dict = {}
        self.pending_performance_reload = False

    def exec(self):
        return self.DialogCode.Accepted


class TestOpenLegacySettingsWindowRolesBroadcast:
    """#510: Save & Apply on the Providers & Roles page persists rolesEnabled
    straight to disk (no `pending_*` staging like the provider toggle) — this
    is the one place that can notice the change and broadcast it to a live
    Lead without touching the settings_window.py widget itself."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import agent_takkub.config as config_mod
        import agent_takkub.pipeline_config as pipeline_config
        import agent_takkub.settings_window as settings_window_mod

        monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
        monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "active_project", lambda: ("myproject", {}))
        monkeypatch.setattr(settings_window_mod, "SettingsWindow", _FakeAcceptedDialog)
        return pipeline_config

    def test_disabling_a_role_notifies_orchestrator(self, _isolate, monkeypatch):
        pipeline_config = _isolate

        def _fake_dialog(parent, project=None, initial_view=None):
            dlg = _FakeAcceptedDialog(parent, project, initial_view)
            payload = pipeline_config.load(project)
            payload["rolesEnabled"]["qa"] = False
            pipeline_config.save(payload, project)
            return dlg

        monkeypatch.setattr("agent_takkub.settings_window.SettingsWindow", _fake_dialog)
        stub = _Stub()

        stub._open_legacy_settings_window(VIEW_PROVIDERS_ROLES)

        stub.orch.notify_roles_changed.assert_called_once_with("myproject", {"qa": True})

    def test_no_role_change_does_not_notify(self, _isolate):
        stub = _Stub()

        stub._open_legacy_settings_window(VIEW_PROVIDERS_ROLES)

        stub.orch.notify_roles_changed.assert_not_called()

    def test_re_enabling_a_role_notifies_with_false(self, _isolate, monkeypatch):
        pipeline_config = _isolate
        payload = pipeline_config.load("myproject")
        payload["rolesEnabled"]["qa"] = False
        pipeline_config.save(payload, "myproject")

        def _fake_dialog(parent, project=None, initial_view=None):
            dlg = _FakeAcceptedDialog(parent, project, initial_view)
            payload = pipeline_config.load(project)
            payload["rolesEnabled"]["qa"] = True
            pipeline_config.save(payload, project)
            return dlg

        monkeypatch.setattr("agent_takkub.settings_window.SettingsWindow", _fake_dialog)
        stub = _Stub()

        stub._open_legacy_settings_window(VIEW_PROVIDERS_ROLES)

        stub.orch.notify_roles_changed.assert_called_once_with("myproject", {"qa": False})
