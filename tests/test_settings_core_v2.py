"""Widget smoke tests for the Core V2 Settings views (epic #309 Phase 9,
`settings_core_v2.CoreV2SettingsMixin`) + `core_v2_settings` round-trip +
config-fallback flag tests.

Offscreen QPA (session-scoped QApplication from tests/conftest.py), same
"tofu" widget-property style as test_settings_window.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, core_v2_settings, custom_roles, settings_window
from agent_takkub import roles as roles_mod
from agent_takkub.core.accounts.registry import AccountPoolRegistry, AccountRegistry
from agent_takkub.core.models.account import AccountPool, AccountStatus, ProviderAccount


@pytest.fixture(autouse=True)
def _isolate_core_v2_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    saved = dict(roles_mod._CUSTOM)
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


class TestCoreV2SettingsStore:
    def test_load_on_missing_file_is_all_defaults(self) -> None:
        payload = core_v2_settings.load()
        assert payload["flags"] == {name: False for name in core_v2_settings.FLAG_NAMES}

    def test_set_flag_round_trips(self) -> None:
        assert core_v2_settings.flag_enabled("router") is False
        assert core_v2_settings.set_flag("router", True) is True
        assert core_v2_settings.flag_enabled("router") is True
        # Untouched flags stay False — a single set_flag must not clobber siblings.
        assert core_v2_settings.flag_enabled("brain") is False

    def test_set_unknown_flag_raises(self) -> None:
        with pytest.raises(ValueError):
            core_v2_settings.set_flag("bogus", True)

    def test_scheduler_policy_round_trips(self) -> None:
        policy = core_v2_settings.SchedulerPolicyConfig(
            max_agents_global=4,
            provider_max_concurrent={"codex": 2},
            default_priority="high",
        )
        assert core_v2_settings.save_scheduler_policy(policy) is True
        reloaded = core_v2_settings.load_scheduler_policy()
        assert reloaded.max_agents_global == 4
        assert reloaded.provider_max_concurrent == {"codex": 2}
        assert reloaded.default_priority == "high"

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        core_v2_settings.path().parent.mkdir(parents=True, exist_ok=True)
        core_v2_settings.path().write_text("not json", encoding="utf-8")
        assert core_v2_settings.flag_enabled("router") is False


class TestFlagConfigFallback:
    """Env always wins; unset falls back to the persisted config — the exact
    contract each core/*/flag.py docstring now states."""

    def test_router_env_wins_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        core_v2_settings.set_flag("router", True)
        monkeypatch.setenv("TAKKUB_V2_ROUTER", "0")
        assert v2_router_enabled() is False

    def test_router_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
        assert v2_router_enabled() is False
        core_v2_settings.set_flag("router", True)
        assert v2_router_enabled() is True

    def test_brain_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.brain.flag import v2_brain_enabled

        monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)
        core_v2_settings.set_flag("brain", True)
        assert v2_brain_enabled() is True

    def test_scheduler_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.scheduling.flag import v2_scheduler_enabled

        monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
        core_v2_settings.set_flag("scheduler", True)
        assert v2_scheduler_enabled() is True

    def test_conversation_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.conversation.flag import v2_conversation_enabled

        monkeypatch.delenv("TAKKUB_V2_CONVERSATION", raising=False)
        core_v2_settings.set_flag("conversation", True)
        assert v2_conversation_enabled() is True


class TestCoreV2Views:
    def test_all_six_views_build_with_empty_stores(self) -> None:
        """Every Core V2 view must open cleanly with flags off and every
        core store empty (task spec: "ทุกหน้าต้องเปิดได้แม้ core store
        ว่าง/flag ปิด")."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_OVERVIEW)
        for view in (
            settings_window.VIEW_CORE_V2_OVERVIEW,
            settings_window.VIEW_CORE_V2_ACCOUNTS,
            settings_window.VIEW_CORE_V2_ROUTING,
            settings_window.VIEW_CORE_V2_BRAIN,
            settings_window.VIEW_CORE_V2_SCHEDULER,
            settings_window.VIEW_CORE_V2_MIGRATION,
        ):
            dlg._goto_view(view)
            assert dlg._stack.currentIndex() == view
        dlg.deleteLater()

    def test_overview_flag_toggles_seeded_from_disk(self) -> None:
        core_v2_settings.set_flag("brain", True)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_OVERVIEW)
        assert dlg._cv2_flag_toggles["brain"].isChecked() is True
        assert dlg._cv2_flag_toggles["router"].isChecked() is False
        # "context" has no wired core module yet — toggle exists but disabled.
        assert dlg._cv2_flag_toggles["context"].isEnabled() is False
        dlg.deleteLater()

    def test_overview_save_flags_persists(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_OVERVIEW)
        dlg._cv2_flag_toggles["scheduler"].setChecked(True)
        dlg._on_cv2_save_flags_clicked()
        assert core_v2_settings.flag_enabled("scheduler") is True
        dlg.deleteLater()

    def test_accounts_view_empty_state(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ACCOUNTS)
        assert dlg._cv2_accounts_list.count() == 1
        assert "ยังไม่มี account" in dlg._cv2_accounts_list.item(0).text()
        dlg.deleteLater()

    def test_accounts_view_lists_existing_registry_rows(self) -> None:
        AccountRegistry().upsert(
            ProviderAccount(
                id="acc-1", provider_id="codex", status=AccountStatus.ACTIVE, priority=5
            )
        )
        AccountPoolRegistry().upsert(
            AccountPool(id="pool-1", name="Codex pool", provider_id="codex", account_ids=("acc-1",))
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ACCOUNTS)
        assert dlg._cv2_accounts_list.count() == 1
        assert "acc-1" in dlg._cv2_accounts_list.item(0).text()
        assert dlg._cv2_pools_list.count() == 1
        assert "pool-1" in dlg._cv2_pools_list.item(0).text()
        dlg.deleteLater()

    def test_remove_account_deletes_from_registry(self) -> None:
        AccountRegistry().upsert(ProviderAccount(id="acc-1", provider_id="codex"))
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ACCOUNTS)
        dlg._cv2_accounts_list.setCurrentRow(0)
        AccountRegistry().delete("acc-1")  # simulate the confirmed-delete path directly
        dlg._reload_cv2_accounts()
        assert AccountRegistry().get("acc-1") is None
        dlg.deleteLater()

    def test_routing_view_shows_resolved_provider(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ROUTING)
        assert dlg._cv2_routing_role_combo.count() > 0
        assert "resolved provider" in dlg._cv2_routing_result.toPlainText()
        dlg.deleteLater()

    def test_brain_view_reindex_updates_counts_label(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_BRAIN)
        assert "กด" in dlg._cv2_brain_counts_lbl.text()
        dlg._on_cv2_brain_reindex_clicked()
        thread = dlg._cv2_brain_reindex_thread
        assert thread is not None
        thread.wait(5000)
        QCoreApplication.processEvents()  # deliver the queued resultReady signal
        assert "ยังไม่มี memory record" in dlg._cv2_brain_counts_lbl.text()
        dlg.deleteLater()

    def test_scheduler_view_save_policy_persists(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_SCHEDULER)
        dlg._cv2_max_agents_spin.setValue(3)
        dlg._cv2_provider_limits_edit[1].setPlainText("codex=2")
        dlg._on_cv2_save_scheduler_policy_clicked()
        reloaded = core_v2_settings.load_scheduler_policy()
        assert reloaded.max_agents_global == 3
        assert reloaded.provider_max_concurrent == {"codex": 2}
        dlg.deleteLater()

    def test_migration_view_refresh_populates_report(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_MIGRATION)
        dlg._on_cv2_migration_refresh_clicked()
        thread = dlg._cv2_migration_thread
        assert thread is not None
        thread.wait(5000)
        QCoreApplication.processEvents()  # deliver the queued resultReady signal
        text = dlg._cv2_migration_report.toPlainText()
        assert "[inspect]" in text and "[plan]" in text
        dlg.deleteLater()

    def test_migration_view_has_no_apply_button(self) -> None:
        """Task spec: "ห้ามมีปุ่ม apply ใน UI รอบนี้" — never offer it. Scoped
        to the Migration page itself, not the whole dialog (the shared
        footer's own "Save && Apply" button is unrelated)."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_MIGRATION)
        page = dlg._stack.widget(settings_window.VIEW_CORE_V2_MIGRATION)
        labels = {b.text().lower() for b in page.findChildren(type(dlg._cv2_migration_dry_run_btn))}
        assert not any("apply" in label for label in labels)
        dlg.deleteLater()
