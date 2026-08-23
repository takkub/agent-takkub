"""Widget smoke tests for the Core V2 Settings views (epic #309 Phase 9,
`settings_core_v2.CoreV2SettingsMixin`) + `core_v2_settings` round-trip +
config-fallback flag tests.

Offscreen QPA (session-scoped QApplication from tests/conftest.py), same
"tofu" widget-property style as test_settings_window.py.
"""

from __future__ import annotations

import json
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
    core_v2_settings._reset_cache()
    saved = dict(roles_mod._CUSTOM)
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)
    core_v2_settings._reset_cache()


class TestCoreV2SettingsStore:
    def test_load_on_missing_file_is_all_defaults(self) -> None:
        payload = core_v2_settings.load()
        # Default-ON since 1.0.84 (epic #309's last rung before 2.0.0) — except
        # `v2_authority` (#362 Phase 10 wave 2), which stays OFF until its own
        # soak, see `core_v2_settings._DEFAULT_FLAGS`'s own comment.
        expected = {name: True for name in core_v2_settings.FLAG_NAMES}
        expected["v2_authority"] = False
        assert payload["flags"] == expected

    def test_set_flag_round_trips(self) -> None:
        assert core_v2_settings.flag_enabled("router") is True  # shipped default
        assert core_v2_settings.set_flag("router", False) is True
        assert core_v2_settings.flag_enabled("router") is False
        # Untouched flags keep the default — a single set_flag must not clobber
        # siblings (the direction that matters now the default is ON).
        assert core_v2_settings.flag_enabled("brain") is True

    def test_every_flag_ships_enabled(self) -> None:
        """The 1.0.84 flip itself (epic #309's last rung before 2.0.0): a
        cockpit with no settings file gets all five original Core V2
        subsystems on. `v2_authority` (#362 Phase 10 wave 2) is deliberately
        excluded — see `test_load_on_missing_file_is_all_defaults`.

        Pinned as its own test because every OTHER flag test now sets the
        value it wants explicitly, so nothing else would notice if the
        shipped default silently regressed to off.
        """
        for name in core_v2_settings.FLAG_NAMES:
            if name == "v2_authority":
                assert core_v2_settings.flag_enabled(name) is False, name
                continue
            assert core_v2_settings.flag_enabled(name) is True, name

    def test_explicit_false_on_disk_survives_the_new_default(self) -> None:
        """An operator who turned a flag OFF must keep it off across the
        upgrade — `load()` layers the persisted file over the defaults, so a
        stored `false` is a decision, not an absence."""
        core_v2_settings.set_flag("scheduler", False)
        assert core_v2_settings.load()["flags"]["scheduler"] is False
        assert core_v2_settings.flag_enabled("scheduler") is False

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
        assert core_v2_settings.flag_enabled("router") is True  # falls back to defaults


class TestCoreV2SettingsCache:
    """`load()` reloads only when `core-v2-settings.json`'s (mtime, size)
    actually changes — PR #311 review must-fix #1."""

    def test_load_twice_unchanged_file_reads_disk_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        core_v2_settings.set_flag("router", True)  # creates the file, primes nothing
        core_v2_settings._reset_cache()

        calls = []
        real_read_text = Path.read_text

        def counting_read_text(self, *a, **kw):
            calls.append(self)
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        first = core_v2_settings.load()
        second = core_v2_settings.load()
        assert first == second
        assert len(calls) == 1  # second load() is a pure cache hit

    def test_load_after_file_edit_rereads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        core_v2_settings.set_flag("router", True)
        assert core_v2_settings.load()["flags"]["router"] is True

        # Edit the file directly (bypassing save()) so mtime/size change
        # without going through the cache-invalidating path — proves load()
        # itself detects the change, not just save()'s _reset_cache().
        raw = json.loads(core_v2_settings.path().read_text(encoding="utf-8"))
        raw["flags"]["router"] = False
        core_v2_settings.path().write_text(json.dumps(raw), encoding="utf-8")

        assert core_v2_settings.load()["flags"]["router"] is False

    def test_set_flag_invalidates_cache_immediately(self) -> None:
        assert core_v2_settings.flag_enabled("brain") is True  # shipped default
        core_v2_settings.set_flag("brain", False)
        assert core_v2_settings.flag_enabled("brain") is False

    def test_missing_file_caches_default_and_still_detects_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert core_v2_settings.load()["flags"]["router"] is True  # cache = (None, defaults)
        assert core_v2_settings.load()["flags"]["router"] is True  # cache hit, no crash

        core_v2_settings.set_flag("router", False)  # file now exists
        assert core_v2_settings.load()["flags"]["router"] is False

    def test_load_returns_independent_copies(self) -> None:
        core_v2_settings.set_flag("router", True)
        first = core_v2_settings.load()
        first["flags"]["router"] = False  # mutate the caller's copy
        second = core_v2_settings.load()
        assert second["flags"]["router"] is True  # cache itself is untouched


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
        assert v2_router_enabled() is True  # no file yet -> shipped default
        core_v2_settings.set_flag("router", False)
        assert v2_router_enabled() is False  # config is what env-unset reads

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
        core_v2_settings.set_flag("brain", False)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_OVERVIEW)
        assert dlg._cv2_flag_toggles["brain"].isChecked() is False
        assert dlg._cv2_flag_toggles["router"].isChecked() is True  # shipped default
        # Every flag in _FLAG_ROWS now has a wired core module — "context"
        # included (Phase 7c: core/brain/context_builder.py, called from
        # orchestrator._assign_dispatch's Context-Injection hook), so no row
        # may ship disabled. Guards the stale `wired=False` this row carried
        # after 7c landed, which left the toggle greyed out in Settings.
        for key in core_v2_settings.FLAG_NAMES:
            assert dlg._cv2_flag_toggles[key].isEnabled() is True, key
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
