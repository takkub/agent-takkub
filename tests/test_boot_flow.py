"""`boot_flow.py` (#574) — provider-update items/choice, migration plan,
progress events, and outcome construction. Migration-side tests operate
under an isolated tmp DATA_HOME, matching test_auto_migrate_boot.py's
fixture."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_takkub import boot_flow, config


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    data_home.mkdir()
    settings_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", data_home / "runtime")
    yield


# ---------------------------------------------------------------------------
# check_provider_updates()
# ---------------------------------------------------------------------------


class TestCheckProviderUpdates:
    def test_disabled_provider_reports_disabled_status(self, monkeypatch):
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(name="foo", display_name="Foo", install_command=None)
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"foo": fake_spec})
        monkeypatch.setattr(
            provider_update,
            "eligibility_gap",
            lambda name: provider_update.UpdateOutcome(
                name, provider_update.STATUS_SKIPPED_DISABLED, "disabled"
            ),
        )
        items = boot_flow.check_provider_updates()
        assert items == [
            boot_flow.ProviderUpdateItem(
                "foo", "Foo", None, None, False, boot_flow.PROVIDER_STATUS_DISABLED
            )
        ]

    def test_not_installed_provider(self, monkeypatch):
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(name="foo", display_name="Foo", install_command=None)
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"foo": fake_spec})
        monkeypatch.setattr(
            provider_update,
            "eligibility_gap",
            lambda name: provider_update.UpdateOutcome(
                name, provider_update.STATUS_SKIPPED_NOT_INSTALLED, "not installed"
            ),
        )
        items = boot_flow.check_provider_updates()
        assert items[0].status == boot_flow.PROVIDER_STATUS_NOT_INSTALLED

    def test_no_latest_probe_reports_up_to_date_not_a_nag(self, monkeypatch):
        """#574 task brief: "ไม่รู้ = latest None + status ตามเดิม" — a
        provider with no known latest-version probe (uv-managed, or no
        mechanism at all) must never be flagged as needing an update it
        can't even check for (matches the mockup: gemini/kimi both show
        up-to-date despite neither having a version-check mechanism)."""
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(
            name="kimi", display_name="Kimi", install_command=["uv", "tool", "install", "kimi-cli"]
        )
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"kimi": fake_spec})
        monkeypatch.setattr(provider_update, "eligibility_gap", lambda name: None)
        monkeypatch.setattr(boot_flow, "_current_version_generic", lambda spec: "1.50.0")
        items = boot_flow.check_provider_updates()
        assert items[0].current == "1.50.0"
        assert items[0].latest is None
        assert items[0].status == boot_flow.PROVIDER_STATUS_UP_TO_DATE
        assert items[0].selected is False

    def test_npm_provider_update_available(self, monkeypatch):
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(
            name="codex",
            display_name="Codex",
            install_command=["npm", "install", "-g", "@openai/codex"],
        )
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"codex": fake_spec})
        monkeypatch.setattr(provider_update, "eligibility_gap", lambda name: None)
        monkeypatch.setattr(boot_flow, "_current_version_generic", lambda spec: "0.154.0")
        monkeypatch.setattr(
            boot_flow, "_npm_view_version", lambda pkg, timeout_s: (True, "0.155.1")
        )
        items = boot_flow.check_provider_updates()
        assert items[0].status == boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE
        assert items[0].selected is True

    def test_npm_up_to_date(self, monkeypatch):
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(
            name="codex",
            display_name="Codex",
            install_command=["npm", "install", "-g", "@openai/codex"],
        )
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"codex": fake_spec})
        monkeypatch.setattr(provider_update, "eligibility_gap", lambda name: None)
        monkeypatch.setattr(boot_flow, "_current_version_generic", lambda spec: "0.155.1")
        monkeypatch.setattr(
            boot_flow, "_npm_view_version", lambda pkg, timeout_s: (True, "0.155.1")
        )
        items = boot_flow.check_provider_updates()
        assert items[0].status == boot_flow.PROVIDER_STATUS_UP_TO_DATE
        assert items[0].selected is False

    def test_npm_probe_failure_reports_failed_status(self, monkeypatch):
        from agent_takkub import provider_update

        fake_spec = SimpleNamespace(
            name="codex",
            display_name="Codex",
            install_command=["npm", "install", "-g", "@openai/codex"],
        )
        monkeypatch.setattr("agent_takkub.provider_spec.PROVIDER_REGISTRY", {"codex": fake_spec})
        monkeypatch.setattr(provider_update, "eligibility_gap", lambda name: None)
        monkeypatch.setattr(boot_flow, "_current_version_generic", lambda spec: "0.154.0")
        monkeypatch.setattr(boot_flow, "_npm_view_version", lambda pkg, timeout_s: (False, None))
        items = boot_flow.check_provider_updates()
        assert items[0].status == boot_flow.PROVIDER_STATUS_FAILED


class TestProviderChoice:
    def test_remember_then_recall_round_trip(self):
        choice = {"mode": "selected", "selected": ["claude", "codex"]}
        boot_flow.remember_provider_choice(choice)
        assert boot_flow.remembered_provider_choice() == choice

    def test_no_choice_yet_returns_none(self):
        assert boot_flow.remembered_provider_choice() is None

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            boot_flow.remember_provider_choice({"mode": "bogus"})

    def test_corrupt_file_reads_as_none(self):
        path = boot_flow._choice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert boot_flow.remembered_provider_choice() is None


class TestRunProviderUpdates:
    def test_unselected_items_pass_through_unchanged(self):
        item = boot_flow.ProviderUpdateItem(
            "foo", "Foo", "1.0", None, False, boot_flow.PROVIDER_STATUS_UP_TO_DATE
        )
        out = boot_flow.run_provider_updates([item])
        assert out == [item]

    def test_selected_item_runs_update_provider_and_maps_status(self, monkeypatch):
        from agent_takkub import provider_update

        monkeypatch.setattr(
            provider_update,
            "update_provider",
            lambda name: provider_update.UpdateOutcome(
                name, provider_update.STATUS_UPDATED, "v2.0.0"
            ),
        )
        item = boot_flow.ProviderUpdateItem(
            "foo", "Foo", "1.0", "2.0", True, boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE
        )
        seen = []
        out = boot_flow.run_provider_updates([item], progress_cb=seen.append)
        assert out[0].status == boot_flow.PROVIDER_STATUS_UP_TO_DATE
        assert out[0].current == "2.0"
        assert seen == out

    def test_a_broken_progress_cb_never_breaks_the_loop(self, monkeypatch):
        from agent_takkub import provider_update

        monkeypatch.setattr(
            provider_update,
            "update_provider",
            lambda name: provider_update.UpdateOutcome(name, provider_update.STATUS_FAILED, "boom"),
        )
        item = boot_flow.ProviderUpdateItem(
            "foo", "Foo", "1.0", "2.0", True, boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE
        )

        def _raise(_it):
            raise RuntimeError("ui crashed")

        out = boot_flow.run_provider_updates([item], progress_cb=_raise)
        assert out[0].status == boot_flow.PROVIDER_STATUS_FAILED


# ---------------------------------------------------------------------------
# plan_migration()
# ---------------------------------------------------------------------------


def _seed_v1_leftover(data_home: Path) -> None:
    (data_home / "projects.json").write_text(json.dumps({"a": {}, "b": {}}), encoding="utf-8")
    (data_home / "custom-roles.json").write_text("{}", encoding="utf-8")


class TestPlanMigration:
    def test_none_once_fully_on_v2(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        assert boot_flow.plan_migration() is None

    def test_plan_lists_promote_and_archive_candidates(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        _seed_v1_leftover(data_home)
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")
        # #574 round11 item 1: a genuine MERGE collision — a top-level
        # `models/` already holding different content than what `v2/models`
        # is about to be promoted into — is the one case still backed up
        # here; a non-colliding promote candidate (the old, wider-scope
        # assumption this test used to make) no longer is.
        (data_home / "models").mkdir(parents=True)
        (data_home / "models" / "registry.json").write_text(
            '{"pre_existing": true}', encoding="utf-8"
        )

        plan = boot_flow.plan_migration()
        assert plan is not None
        assert "models" in plan.promote_items
        assert "custom-roles.json" in plan.archive_items
        assert plan.backup_dir.parent.name == "backups"
        assert any(label == "models" for label, _count, _bytes, _unit in plan.backup_items)
        assert plan.estimated_bytes >= 0
        assert plan.free_bytes >= 0

    def test_verify_steps_is_the_real_ladder_length_not_promote_items(self, monkeypatch):
        """#504/#574 round10: `verify_steps` must track the actual ladder
        (`MigrationEngine.step_count()`) — the same count a later
        `validate()` pass reports as `MigrationOutcome.failed_step_total` —
        never `len(promote_items)`, which is only ONE step's own
        candidate count and would drift the moment either number changes
        independently of the other."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub.core.migration.engine import MigrationEngine

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        _seed_v1_leftover(data_home)
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        plan = boot_flow.plan_migration()
        assert plan is not None
        assert plan.verify_steps == MigrationEngine().step_count()
        assert plan.verify_steps != len(plan.promote_items)


# ---------------------------------------------------------------------------
# run_migration() / MigrationOutcome
# ---------------------------------------------------------------------------


class TestRunMigrationOutcome:
    def test_plan_none_short_circuits_with_no_progress_noise(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        monkeypatch.setattr(
            auto_migrate_boot,
            "run_boot_stage",
            lambda *a, **k: auto_migrate_boot.BootMigrationResult(
                "skipped", messages=["nothing to do"]
            ),
        )
        events = []
        outcome = boot_flow.run_migration(progress_cb=events.append)
        assert outcome.ok
        assert events == []

    def test_stale_failure_reports_ok_false_even_though_action_says_pending_applied(
        self, monkeypatch
    ):
        """#574 round6 R6-H1: `auto_migrate_boot` deliberately keeps
        action='pending_applied' for a stale step's failed re-attempt (by
        design — never auto-rolled-back). `MigrationOutcome.ok` must not
        repeat that as an unqualified success — it has `result.reports` to
        check instead."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot
        from agent_takkub.core.migration.report import StepReport

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        result = auto_migrate_boot.BootMigrationResult(
            "pending_applied",
            messages=["pending step(s) apply สำเร็จ"],
            reports=[
                StepReport("promote-v2-root", "apply", False, "missing both source and target")
            ],
        )
        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", lambda *a, **k: result)
        monkeypatch.setattr(boot_flow, "plan_migration", lambda: None)

        outcome = boot_flow.run_migration()
        assert outcome.ok is False
        assert outcome.failed_step == "promote-v2-root"
        assert outcome.failed_phase == 2
        assert outcome.data_intact is True  # copy_phase's own undo already ran

    def test_three_consecutive_stale_failures_converge_on_the_same_honest_result(self, monkeypatch):
        """#574 round6 R6-H1 DoD: boot x3 after a rollback must converge —
        either validate goes green, or every boot reports the SAME single
        clear failure, never a false pending_applied success."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot
        from agent_takkub.core.migration.report import StepReport

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        result = auto_migrate_boot.BootMigrationResult(
            "pending_applied",
            messages=[],
            reports=[StepReport("archive-v1-legacy", "apply", False, "missing both")],
        )
        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", lambda *a, **k: result)
        monkeypatch.setattr(boot_flow, "plan_migration", lambda: None)

        outcomes = [boot_flow.run_migration() for _ in range(3)]
        assert all(o.ok is False for o in outcomes)
        assert all(o.failed_step == "archive-v1-legacy" for o in outcomes)
        assert all(o.failed_phase == 4 for o in outcomes)

    def test_rolled_back_action_with_failed_rollback_marks_data_not_intact(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        result = auto_migrate_boot.BootMigrationResult(
            "rolled_back",
            reason="'state' ไม่ผ่าน",
            messages=["rollback ไม่สำเร็จ — ต้องตรวจด้วยมือ"],
        )
        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", lambda *a, **k: result)
        monkeypatch.setattr(boot_flow, "plan_migration", lambda: None)

        outcome = boot_flow.run_migration()
        assert outcome.ok is False
        assert outcome.rolled_back is True
        assert outcome.data_intact is False

    def test_progress_events_flow_through_on_entry_and_text_phases(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        captured = {}

        def fake_run_boot_stage(*, progress_cb=None, on_entry=None, on_file_progress=None):
            captured["on_entry"] = on_entry
            on_entry("promote-v2-root", "models")
            progress_cb("apply สำเร็จ — กำลัง validate…")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        outcome = boot_flow.run_migration(progress_cb=events.append)
        assert outcome.ok
        assert captured["on_entry"] is not None
        phases_seen = {e.phase for e in events}
        assert {1, 2, 3, 5} <= phases_seen
        assert all(0.0 <= e.percent_overall <= 100.0 for e in events)
        assert events[-1].percent_overall == 100.0
        # #504/#574 round10: the per-entry event fired via on_entry carries
        # the entry's own name as `current_path` (short, relative to
        # DATA_HOME) — every other event (no single item behind it) is None.
        entry_events = [e for e in events if e.log_line == "promote-v2-root: models"]
        assert entry_events and all(e.current_path == "models" for e in entry_events)
        other_events = [e for e in events if e.log_line != "promote-v2-root: models"]
        assert all(e.current_path is None for e in other_events)

    def test_a_broken_progress_cb_never_breaks_the_run(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        (config.DATA_HOME / "v2" / "models").mkdir(parents=True)
        (config.DATA_HOME / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        def fake_run_boot_stage(*, progress_cb=None, on_entry=None, on_file_progress=None):
            on_entry("promote-v2-root", "models")
            progress_cb("x")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        def _raise(_event):
            raise RuntimeError("ui crashed")

        outcome = boot_flow.run_migration(progress_cb=_raise)
        assert outcome.ok
