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
from PyQt6.QtWidgets import QLineEdit

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
