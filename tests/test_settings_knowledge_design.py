"""Widget smoke tests for the Knowledge & Design Settings views
(`settings_knowledge_design.KnowledgeDesignSettingsMixin` — final closeout
pack 2, `docs/plans/final-closeout-after-1.3.0/04_SETTINGS_UI_FINAL.md`).

Offscreen QPA (session-scoped QApplication from tests/conftest.py), same
"tofu" widget-property + `thread.wait()` + `QCoreApplication.processEvents()`
style `test_settings_core_v2.py` already uses for its own worker-thread
buttons. Every network/subprocess-touching function (`openviking_adapter.
health`, `doctor.check_graft`, `PenpotClient.get_profile`, `detect_
storybook`, `integration_config_status`) is monkeypatched to a fake — no
test here ever makes a real socket/subprocess call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QLineEdit, QMessageBox

from agent_takkub import (
    config,
    custom_roles,
    openviking_settings,
    pane_tools_policy,
    settings_window,
)
from agent_takkub import roles as roles_mod
from agent_takkub.core.capabilities import design_integrations
from agent_takkub.core.context_sources import openviking_adapter
from agent_takkub.core.secrets.manager import SecretManager
from agent_takkub.openviking import installer as ov_installer
from agent_takkub.openviking import manager as ov_manager
from agent_takkub.openviking import process as ov_process


@pytest.fixture(autouse=True)
def _isolate_kd_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    saved = dict(roles_mod._CUSTOM)
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


@pytest.fixture(autouse=True)
def _isolate_openviking_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "ov-home"
    monkeypatch.setattr(ov_installer, "OPENVIKING_HOME", home)
    monkeypatch.setattr(ov_installer, "VENV_DIR", home / "venv")
    monkeypatch.setattr(ov_installer, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(ov_installer, "CONFIG_FILE", home / "config" / "ov.conf")
    monkeypatch.setattr(ov_installer, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(ov_installer, "DATA_DIR", home / "data")
    monkeypatch.setattr(ov_process, "LOG_FILE", home / "logs" / "openviking.log")
    monkeypatch.setattr(ov_manager, "_instance", None)


def _wait(thread) -> None:
    assert thread is not None
    thread.wait(5000)
    QCoreApplication.processEvents()


class TestNavigation:
    def test_all_four_views_reachable(self) -> None:
        dlg = settings_window.SettingsWindow()
        for view_idx, title in (
            (settings_window.VIEW_KNOWLEDGE, "Knowledge"),
            (settings_window.VIEW_OPENVIKING, "OpenViking"),
            (settings_window.VIEW_DESIGN_TOOLS, "Design Tools"),
            (settings_window.VIEW_CONTEXT_DEBUG, "Context Debug"),
        ):
            dlg._goto_view(view_idx)
            assert dlg._stack.currentIndex() == view_idx
            assert dlg._content_title.text() == title
        dlg.deleteLater()

    def test_knowledge_design_views_disable_footer_save_and_reset(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        assert dlg._save_btn.isEnabled() is False
        assert dlg._reset_btn.isEnabled() is False
        dlg.deleteLater()


class TestOpenVikingView:
    def test_save_settings_round_trips_through_store(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._kd_ov_mode_combo.setCurrentText("hybrid")
        dlg._kd_ov_strict_check.setChecked(False)
        dlg._kd_ov_include_global_check.setChecked(False)
        dlg._kd_ov_limit_spin.setValue(15)
        dlg._kd_ov_timeout_spin.setValue(9.5)
        dlg._on_kd_ov_save_clicked()

        reloaded = openviking_settings.load()
        assert reloaded.mode == "hybrid"
        assert reloaded.strict_project is False
        assert reloaded.include_global is False
        assert reloaded.result_limit == 15
        assert reloaded.timeout == 9.5
        assert "บันทึกแล้ว" in dlg._kd_ov_save_status.text()
        dlg.deleteLater()

    def test_test_button_uses_fake_health_never_touches_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = openviking_adapter.HealthStatus(
            ok=True, healthy=True, version="0.3.1", known_version=True
        )
        monkeypatch.setattr(openviking_adapter, "health", lambda timeout=4.0: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_test_clicked()
        _wait(dlg._kd_ov_thread)

        assert "Connected" in dlg._kd_ov_status_lbl.text()
        assert "0.3.1" in dlg._kd_ov_status_lbl.text()
        dlg.deleteLater()

    def test_sync_active_project_calls_index_vault_incrementally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.context_sources import indexing

        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            indexing, "reset_state", lambda project: calls.append(("reset", project))
        )
        monkeypatch.setattr(
            indexing,
            "index_vault",
            lambda project: (
                calls.append(("index", project))
                or indexing.IndexResult(ok=True, added=2, skipped=1, total=3)
            ),
        )

        dlg = settings_window.SettingsWindow(
            project="agent-takkub", initial_view=settings_window.VIEW_OPENVIKING
        )
        dlg._on_kd_ov_sync_clicked()
        _wait(dlg._kd_ov_thread)

        assert calls == [("index", "agent-takkub")]  # no reset — Sync stays incremental
        assert "added=2" in dlg._kd_ov_result.toPlainText()
        dlg.deleteLater()

    def test_reindex_resets_state_before_indexing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.core.context_sources import indexing

        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            indexing, "reset_state", lambda project: calls.append(("reset", project))
        )
        monkeypatch.setattr(
            indexing,
            "index_vault",
            lambda project: calls.append(("index", project)) or indexing.IndexResult(ok=True),
        )

        dlg = settings_window.SettingsWindow(
            project="agent-takkub", initial_view=settings_window.VIEW_OPENVIKING
        )
        dlg._on_kd_ov_reindex_clicked()
        _wait(dlg._kd_ov_thread)

        assert calls == [("reset", "agent-takkub"), ("index", "agent-takkub")]
        dlg.deleteLater()


class TestDesignToolsView:
    def test_credential_field_is_masked(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        assert dlg._kd_design_token_edit.echoMode() == QLineEdit.EchoMode.Password
        dlg.deleteLater()

    def test_save_credential_writes_through_secret_manager_and_clears_field(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        idx = dlg._kd_design_target_combo.findData("figma")
        dlg._kd_design_target_combo.setCurrentIndex(idx)
        dlg._kd_design_token_edit.setText("sekret-token-123")
        dlg._on_kd_design_save_credential_clicked()

        assert dlg._kd_design_token_edit.text() == ""  # never echoed back
        assert SecretManager().get_secret("secret://figma/default") == "sekret-token-123"
        assert "figma" in dlg._kd_design_cred_status.text()
        dlg.deleteLater()


class _FakeManagerHandle:
    """Stand-in for `openviking.manager.OpenVikingManager` — no real
    install/spawn ever runs (`start`/`stop`/`restart`/`ensure_installed` are
    all recorded calls returning a canned `ManagerStatus`)."""

    def __init__(self, status: ov_manager.ManagerStatus) -> None:
        self._status = status
        self.start_called = False
        self.stop_called = False
        self.restart_called = False
        self.ensure_installed_calls: list[bool] = []

    def status(self) -> ov_manager.ManagerStatus:
        return self._status

    def start(self) -> ov_manager.ManagerStatus:
        self.start_called = True
        return self._status

    def stop(self) -> None:
        self.stop_called = True

    def restart(self) -> ov_manager.ManagerStatus:
        self.restart_called = True
        return self._status

    def ensure_installed(self, *, force: bool = False) -> bool:
        self.ensure_installed_calls.append(force)
        return True


class TestOpenVikingManagedRuntimeView:
    def test_not_installed_shows_install_enable_and_hides_detail(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)

        assert dlg._kd_ov_install_enable_btn.isHidden() is False
        assert dlg._kd_ov_managed_detail_host.isHidden() is True
        assert dlg._kd_ov_auto_start_check.isHidden() is True
        dlg.deleteLater()

    def test_installed_hides_install_enable_and_shows_detail(self) -> None:
        exe = ov_installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)

        assert dlg._kd_ov_install_enable_btn.isHidden() is True
        assert dlg._kd_ov_managed_detail_host.isHidden() is False
        assert dlg._kd_ov_auto_start_check.isHidden() is False
        dlg.deleteLater()

    def test_refresh_updates_status_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_status = ov_manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        fake = _FakeManagerHandle(fake_status)
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_managed_refresh_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert "Running" in dlg._kd_ov_managed_status_lbl.text()
        assert dlg._kd_ov_managed_address_lbl.text() == "http://127.0.0.1:1933"
        assert dlg._kd_ov_managed_runtime_lbl.text() == "Managed by Takkub"
        assert dlg._kd_ov_managed_install_lbl.text() == "Healthy"
        # a healthy result flips visibility to the installed layout even
        # though the on-disk executable check at construction time saw none
        assert dlg._kd_ov_install_enable_btn.isHidden() is True
        dlg.deleteLater()

    def test_open_studio_disabled_before_any_refresh(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        assert dlg._kd_ov_open_studio_btn.isEnabled() is False
        dlg.deleteLater()

    def test_open_studio_disabled_when_status_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, False, False, None, True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_managed_refresh_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert dlg._kd_ov_open_studio_btn.isEnabled() is False
        assert "start" in dlg._kd_ov_open_studio_btn.toolTip().lower()
        dlg.deleteLater()

    def test_open_studio_enabled_when_healthy_and_opens_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeManagerHandle(
            ov_manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        )
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)
        opened_urls: list[str] = []
        monkeypatch.setattr(
            "agent_takkub.settings_knowledge_design.QDesktopServices.openUrl",
            lambda url: opened_urls.append(url.toString()),
        )

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_managed_refresh_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert dlg._kd_ov_open_studio_btn.isEnabled() is True
        assert dlg._kd_ov_open_studio_btn.toolTip() == ""
        dlg._on_kd_ov_open_studio_clicked()

        assert opened_urls == ["http://127.0.0.1:1933/studio"]
        dlg.deleteLater()

    def test_update_button_calls_installer_and_shows_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)
        update_calls = 0

        def _fake_update() -> ov_installer.UpdateResult:
            nonlocal update_calls
            update_calls += 1
            return ov_installer.UpdateResult(previous_version="0.3.0", new_version="0.3.1")

        monkeypatch.setattr(ov_installer, "update", _fake_update)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_update_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert update_calls == 1
        assert "0.3.0" in dlg._kd_ov_managed_status_msg.text()
        assert "0.3.1" in dlg._kd_ov_managed_status_msg.text()
        dlg.deleteLater()

    def test_update_button_shows_major_version_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)
        monkeypatch.setattr(
            ov_installer,
            "update",
            lambda: ov_installer.UpdateResult(
                previous_version="0.3.0", new_version="1.0.0", warning="major version change"
            ),
        )

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_update_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert "คำเตือน" in dlg._kd_ov_managed_status_msg.text()
        dlg.deleteLater()

    def test_update_button_reports_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        def _raise() -> ov_installer.UpdateResult:
            raise ov_installer.InstallerError("boom")

        monkeypatch.setattr(ov_installer, "update", _raise)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_update_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert "ไม่สำเร็จ" in dlg._kd_ov_managed_status_msg.text()
        dlg.deleteLater()

    def test_repair_calls_ensure_installed_with_force(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_repair_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert fake.ensure_installed_calls == [True]
        dlg.deleteLater()

    def test_remove_confirmed_stops_and_uninstalls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(False, False, False, None, False))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes
        )
        uninstall_calls: list[dict] = []
        monkeypatch.setattr(ov_installer, "uninstall", lambda **kw: uninstall_calls.append(kw))

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_remove_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert fake.stop_called is True
        assert uninstall_calls == [{"remove_data": True}]
        dlg.deleteLater()

    def test_remove_cancelled_touches_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(False, False, False, None, False))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.No)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_remove_clicked()

        assert fake.stop_called is False
        dlg.deleteLater()

    def test_service_buttons_call_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)

        dlg._on_kd_ov_service_start_clicked()
        _wait(dlg._kd_ov_managed_thread)
        assert fake.start_called is True

        dlg._on_kd_ov_service_stop_clicked()
        _wait(dlg._kd_ov_managed_thread)
        assert fake.stop_called is True

        dlg._on_kd_ov_service_restart_clicked()
        _wait(dlg._kd_ov_managed_thread)
        assert fake.restart_called is True
        dlg.deleteLater()

    def test_view_logs_shows_log_file_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("PyQt6.QtWidgets.QDialog.exec", lambda self: 0)
        ov_process.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ov_process.LOG_FILE.write_text("hello from openviking\n", encoding="utf-8")

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_view_logs_clicked()  # must not raise / block
        dlg.deleteLater()

    def test_view_logs_with_no_file_shows_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("PyQt6.QtWidgets.QDialog.exec", lambda self: 0)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_view_logs_clicked()  # must not raise / block
        dlg.deleteLater()

    def test_auto_start_toggle_persists(self) -> None:
        exe = ov_installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        assert openviking_settings.load().start_automatically is False

        dlg._kd_ov_auto_start_check.setChecked(True)

        assert openviking_settings.load().start_automatically is True
        dlg.deleteLater()

    def test_install_enable_opens_wizard_and_refreshes_on_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.openviking_setup_dialog as ov_dialog_mod

        monkeypatch.setattr(ov_dialog_mod.OpenVikingSetupDialog, "exec", lambda self: 0)
        fake = _FakeManagerHandle(ov_manager.ManagerStatus(True, True, True, "u", True))
        monkeypatch.setattr(ov_manager, "get_manager", lambda: fake)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_OPENVIKING)
        dlg._on_kd_ov_install_enable_clicked()
        _wait(dlg._kd_ov_managed_thread)

        assert dlg._kd_ov_managed_status_lbl.text() == "● Running"
        dlg.deleteLater()

    def test_save_credential_encodes_penpot_as_json_with_base_url(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        idx = dlg._kd_design_target_combo.findData("penpot")
        dlg._kd_design_target_combo.setCurrentIndex(idx)
        dlg._kd_design_token_edit.setText("tok")
        dlg._kd_design_base_url_edit.setText("https://penpot.example")
        dlg._on_kd_design_save_credential_clicked()

        stored = json.loads(SecretManager().get_secret("secret://penpot/default"))
        assert stored == {"token": "tok", "base_url": "https://penpot.example"}
        dlg.deleteLater()

    def test_save_credential_rejects_penpot_without_base_url(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        idx = dlg._kd_design_target_combo.findData("penpot")
        dlg._kd_design_target_combo.setCurrentIndex(idx)
        dlg._kd_design_token_edit.setText("tok")
        dlg._on_kd_design_save_credential_clicked()

        assert "Base URL" in dlg._kd_design_cred_status.text()
        assert SecretManager().status("secret://penpot/default").name == "MISSING"
        dlg.deleteLater()

    def test_refresh_status_uses_fakes_never_touches_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            design_integrations,
            "detect_storybook",
            lambda roots: design_integrations.StorybookStatus(detected=True, root="/x", port=6006),
        )
        monkeypatch.setattr(
            design_integrations,
            "integration_config_status",
            lambda mcp_id, secret_manager=None: (mcp_id == "figma", "fake status"),
        )

        dlg = settings_window.SettingsWindow(
            project="agent-takkub", initial_view=settings_window.VIEW_DESIGN_TOOLS
        )
        dlg._on_kd_design_refresh_clicked()
        _wait(dlg._kd_design_thread)

        assert "6006" in dlg._kd_design_rows["Storybook"][1].text()
        assert dlg._kd_design_rows["Figma"][1].text() == "fake status"
        dlg.deleteLater()

    def test_test_button_reports_penpot_connectivity_via_fake_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.capabilities import design_clients

        SecretManager().set_secret(
            "secret://penpot/default", json.dumps({"token": "tok", "base_url": "https://x"})
        )
        monkeypatch.setattr(
            design_integrations,
            "integration_config_status",
            lambda mcp_id, secret_manager=None: (mcp_id == "penpot", "ok"),
        )
        fake_profile = design_clients.PenpotProfile(
            id="1",
            fullname="Test User",
            email="test@example.com",
            provenance=design_clients.Provenance(
                source="penpot", url="https://x", license=None, fetched_at="now"
            ),
        )
        monkeypatch.setattr(design_clients.PenpotClient, "get_profile", lambda self: fake_profile)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        dlg._on_kd_design_test_clicked()
        _wait(dlg._kd_design_thread)

        text = dlg._kd_design_result.toPlainText()
        assert "Test User" in text
        assert "not configured" in text  # the other two integrations stay unconfigured
        dlg.deleteLater()

    def test_permissions_dialog_toggle_writes_role_policy(self) -> None:
        from agent_takkub.settings_knowledge_design import _RolePermissionsDialog

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_DESIGN_TOOLS)
        perm = _RolePermissionsDialog(dlg, fonts=dlg._fonts)
        assert ("frontend", "figma") in perm._checks

        cb = perm._checks[("frontend", "figma")]
        assert cb.isChecked() is False
        cb.setChecked(True)
        assert "figma" in (pane_tools_policy.effective_mcps("frontend", frozenset()) or frozenset())

        cb.setChecked(False)
        assert "figma" not in (
            pane_tools_policy.effective_mcps("frontend", frozenset()) or frozenset()
        )
        perm.deleteLater()
        dlg.deleteLater()


class TestKnowledgeView:
    def test_refresh_uses_fakes_never_touches_subprocess_or_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import doctor
        from agent_takkub.core.brain import store as brain_store_mod
        from agent_takkub.core.context_sources import indexing

        monkeypatch.setattr(brain_store_mod.BrainStore, "load_active", lambda self: [1, 2, 3])
        monkeypatch.setattr(doctor, "check_obsidian", lambda: [])
        monkeypatch.setattr(
            doctor,
            "check_graft",
            lambda: [
                doctor.Finding("graft", "cli", doctor.Status.OK, "1.2.3 /usr/bin/graft"),
                doctor.Finding(
                    "graft", "store-size", doctor.Status.OK, "2 live store(s), 4 MB total"
                ),
            ],
        )
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(
            indexing,
            "index_status",
            lambda project: {
                "healthy": True,
                "mode": "shadow",
                "version": "0.3.1",
                "indexed_count": 7,
            },
        )

        dlg = settings_window.SettingsWindow(
            project="agent-takkub", initial_view=settings_window.VIEW_KNOWLEDGE
        )
        dlg._on_kd_knowledge_refresh_clicked()
        _wait(dlg._kd_knowledge_thread)

        assert "3 record" in dlg._kd_knowledge_rows["Brain"][1].text()
        assert "2 live store" in dlg._kd_knowledge_rows["Graft"][1].text()
        assert "indexed=7" in dlg._kd_knowledge_rows["OpenViking"][1].text()
        dlg.deleteLater()


class TestContextDebugView:
    def test_no_trace_shows_placeholder_and_disables_buttons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.context_sources import trace_store

        monkeypatch.setattr(trace_store, "load_last_trace", lambda: None)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CONTEXT_DEBUG)

        assert dlg._kd_ctx_view_btn.isEnabled() is False
        assert "ยังไม่มี" in dlg._kd_ctx_totals_lbl.text()
        dlg.deleteLater()

    def test_trace_renders_table_totals_and_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.core.context_sources import trace_store

        fake_trace = {
            "project": "agent-takkub",
            "role": "frontend",
            "mode": "hybrid",
            "sources": [
                {"name": "OpenViking", "count": 5, "unit": "resources", "tokens": 1842},
                {"name": "Resource", "count": 3, "unit": "docs", "tokens": 731},
            ],
            "total_tokens": 4120,
            "budget_tokens": 6000,
            "dedup_count": 3,
            "latency_ms": 91.0,
        }
        monkeypatch.setattr(trace_store, "load_last_trace", lambda: fake_trace)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CONTEXT_DEBUG)
        assert dlg._kd_ctx_view_btn.isEnabled() is True
        assert "frontend" in dlg._kd_ctx_header_lbl.text()
        assert "4120" in dlg._kd_ctx_totals_lbl.text()
        # scope_rejects/task_size don't exist on this trace shape yet
        # (pane B/C follow-up) — must degrade to "—", never KeyError/crash.
        assert "—" in dlg._kd_ctx_totals_lbl.text()

        report = dlg._kd_ctx_report_text()
        assert "OpenViking" in report and "1842" in report

        dlg._on_kd_ctx_copy_report_clicked()
        from PyQt6.QtGui import QGuiApplication

        assert "agent-takkub" in QGuiApplication.clipboard().text()
        dlg.deleteLater()

    def test_trace_with_forward_compat_fields_renders_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once pane B/C lands `scope_rejects`/`task_size`/per-source
        `time_ms`, this view must pick them up with no code change — only
        the `.get(...)` defaults stop applying."""
        from agent_takkub.core.context_sources import trace_store

        fake_trace = {
            "sources": [{"name": "Brain", "count": 4, "tokens": 945, "time_ms": 10.0}],
            "total_tokens": 945,
            "budget_tokens": 6000,
            "dedup_count": 0,
            "scope_rejects": 4,
            "trust_rejects": 1,
            "task_size": "medium",
        }
        monkeypatch.setattr(trace_store, "load_last_trace", lambda: fake_trace)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CONTEXT_DEBUG)
        assert "Scope rejected: 4" in dlg._kd_ctx_totals_lbl.text()
        assert "Task size: medium" in dlg._kd_ctx_totals_lbl.text()
        assert "10ms" in dlg._kd_ctx_report_text()
        dlg.deleteLater()
