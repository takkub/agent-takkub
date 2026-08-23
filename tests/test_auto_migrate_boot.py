"""Auto `migrate apply` at boot (#361) — pre-flight gate, happy path,
validate-fail -> auto-rollback, dev-checkout skip, mixed-state pending-step
apply (#362), toggle-off skip, and the retry-guards (whole-version and
per-step) that stop a rolled-back attempt from looping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import auto_migrate_boot, config, core_v2_settings
from agent_takkub.core.migration.engine import MigrationEngine
from agent_takkub.core.migration.report import StepReport
from agent_takkub.core.storage.layout import layout_state


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    data_home.mkdir()
    settings_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", data_home / "runtime")
    core_v2_settings._reset_cache()
    yield
    core_v2_settings._reset_cache()


# ---------------------------------------------------------------------------
# auto_migrate_enabled() — env / Settings precedence
# ---------------------------------------------------------------------------


class TestAutoMigrateEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_AUTO_MIGRATE", raising=False)
        assert auto_migrate_boot.auto_migrate_enabled() is True

    def test_env_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_AUTO_MIGRATE", "0")
        assert auto_migrate_boot.auto_migrate_enabled() is False

    def test_env_wins_over_settings_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        core_v2_settings.set_flag("auto_migrate", False)
        monkeypatch.setenv("TAKKUB_AUTO_MIGRATE", "1")
        assert auto_migrate_boot.auto_migrate_enabled() is True

    def test_settings_toggle_off_disables_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_AUTO_MIGRATE", raising=False)
        core_v2_settings.set_flag("auto_migrate", False)
        assert auto_migrate_boot.auto_migrate_enabled() is False


class TestDevCheckoutGate:
    def test_dev_checkout_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
        assert auto_migrate_boot.is_dev_checkout() is True

    def test_normal_install_is_not_dev_checkout(self) -> None:
        assert auto_migrate_boot.is_dev_checkout() is False


# ---------------------------------------------------------------------------
# disk gate
# ---------------------------------------------------------------------------


class TestDiskGate:
    def test_no_runtime_dir_estimates_zero_bytes(self) -> None:
        assert auto_migrate_boot._estimate_copy_bytes(config.DATA_HOME) == 0

    def test_sums_runtime_dir_file_sizes(self) -> None:
        runtime = config.DATA_HOME / "runtime" / "sessions"
        runtime.mkdir(parents=True)
        (runtime / "a.txt").write_bytes(b"x" * 100)
        (runtime / "b.txt").write_bytes(b"y" * 50)
        assert auto_migrate_boot._estimate_copy_bytes(config.DATA_HOME) == 150

    def test_room_available_passes(self) -> None:
        assert auto_migrate_boot._disk_has_room(config.DATA_HOME) is True

    def test_unmeasurable_disk_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_path):
            raise OSError("no such device")

        monkeypatch.setattr(auto_migrate_boot.shutil, "disk_usage", _boom)
        assert auto_migrate_boot._disk_has_room(config.DATA_HOME) is False


# ---------------------------------------------------------------------------
# run_boot_stage — the full gate
# ---------------------------------------------------------------------------


class TestRunBootStageGates:
    def test_disabled_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_AUTO_MIGRATE", "0")
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "skipped"
        assert result.reason == "disabled"

    def test_dev_checkout_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "skipped"
        assert result.reason == "dev-checkout"

    def test_v2_layout_state_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `layout_state` is imported inside `run_boot_stage` — patch the
        # source module's binding, not a module-level name in this one.
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "skipped"
        assert result.reason == "layout-state-v2"

    def test_previously_rolled_back_same_version_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import __version__ as app_version

        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"rolled_back_for_version": app_version}), encoding="utf-8"
        )
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "skipped"
        assert result.reason == "previously-rolled-back"

    def test_rolled_back_for_a_different_version_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"rolled_back_for_version": "0.0.1-not-the-running-build"}),
            encoding="utf-8",
        )
        result = auto_migrate_boot.run_boot_stage()
        assert result.action in ("applied", "rolled_back")  # actually attempted, not skipped

    def test_disk_gate_failure_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auto_migrate_boot, "_disk_has_room", lambda _home: False)
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "skipped"
        assert result.reason == "disk-space"


class TestRunBootStageMixedPendingApply:
    """#362: a `"mixed"` boot must never re-run the full ladder, but it also
    must not be limited to the old step-1-only fast path — any ladder step
    this machine hasn't successfully finished yet gets a chance too."""

    def test_mixed_state_calls_apply_pending_never_the_full_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        calls: list[tuple] = []
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: [])
        monkeypatch.setattr(
            MigrationEngine,
            "apply_pending",
            lambda self, **kw: (
                calls.append(("apply_pending", kw.get("skip_step_ids")))
                or [StepReport("core-internal-store", "apply", True, "ok")]
            ),
        )
        monkeypatch.setattr(
            MigrationEngine, "apply", lambda self: calls.append(("full_apply",)) or []
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"
        assert calls == [("apply_pending", set())]

    def test_mixed_state_nothing_pending_reports_pending_applied_with_no_steps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: ["version-marker"])
        monkeypatch.setattr(MigrationEngine, "apply_pending", lambda self, **kw: [])

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"

    def test_new_pending_step_failure_rolls_back_only_that_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        # This machine already has "state" applied — "core-internal-store"
        # has never been applied here before (the #360 gap).
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: ["state"])
        monkeypatch.setattr(
            MigrationEngine,
            "apply_pending",
            lambda self, **kw: [StepReport("core-internal-store", "apply", False, "disk full")],
        )
        rollback_calls: list[str] = []
        monkeypatch.setattr(
            MigrationEngine,
            "rollback_step",
            lambda self, step_id: (
                rollback_calls.append(step_id) or StepReport(step_id, "rollback", True, "restored")
            ),
        )
        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_rolled_back"
        assert rollback_calls == ["core-internal-store"]
        assert events and events[0] == {
            "event": "auto_migrate_rolled_back",
            "step_id": "core-internal-store",
            "failing_summary": "disk full",
            "rollback_ok": True,
        }

        from agent_takkub import __version__ as app_version

        assert auto_migrate_boot.load_state()["rolled_back_steps"] == {
            "core-internal-store": app_version
        }

    def test_new_pending_step_retry_guard_skips_it_on_the_next_boot_same_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: [])
        calls: list[set] = []
        monkeypatch.setattr(
            MigrationEngine,
            "apply_pending",
            lambda self, **kw: calls.append(kw.get("skip_step_ids")) or [],
        )
        from agent_takkub import __version__ as app_version

        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"rolled_back_steps": {"core-internal-store": app_version}}),
            encoding="utf-8",
        )

        auto_migrate_boot.run_boot_stage()

        assert calls == [{"core-internal-store"}]

    def test_stale_applied_step_does_not_roll_back_and_is_recorded_for_doctor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A step this machine already applied successfully before, whose
        validate() now says it's broken, must never be auto-rolled-back —
        only logged and surfaced via state for `doctor --storage-layout`."""
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: ["state"])
        monkeypatch.setattr(
            MigrationEngine,
            "apply_pending",
            lambda self, **kw: [StepReport("state", "apply", False, "target missing")],
        )
        rollback_calls: list[str] = []
        monkeypatch.setattr(
            MigrationEngine,
            "rollback_step",
            lambda self, step_id: (
                rollback_calls.append(step_id) or StepReport(step_id, "rollback", True, "restored")
            ),
        )
        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"  # not rolled back — this isn't a boot failure
        assert rollback_calls == []
        assert events == [
            {
                "event": "auto_migrate_pending_step_stale",
                "step_id": "state",
                "summary": "target missing",
            }
        ]
        assert auto_migrate_boot.load_state()["stale_applied_steps"] == {"state": "target missing"}

    def test_prod_today_machine_applies_only_the_new_core_internal_store_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end with the real ladder (#362): a machine that migrated
        under the pre-#360 ladder (8 steps: version-marker + the 7 V1
        steps) must, on its next mixed-state boot after upgrading to a
        build carrying #360's core-internal-store, apply just that one new
        step — the other 8 stay untouched, no stale/rollback noise at all."""
        from agent_takkub.core.migration.backup import BackupManager
        from agent_takkub.core.migration.journal import MigrationJournal
        from agent_takkub.core.migration.steps import VersionMarkerStep
        from agent_takkub.core.migration.steps_v1 import (
            CredentialReferenceStep,
            ProjectMigrationStep,
            RoleAgentMigrationStep,
            RuntimeTriageStep,
            build_capability_step,
            build_readonly_registries_step,
            build_state_step,
        )
        from agent_takkub.core.storage.paths import core_home

        journal = MigrationJournal()
        backups = BackupManager()
        pre_360_steps = [
            VersionMarkerStep(journal=journal, backups=backups),
            build_readonly_registries_step(journal, backups),
            RoleAgentMigrationStep(journal=journal, backups=backups),
            build_capability_step(journal, backups),
            ProjectMigrationStep(journal=journal, backups=backups),
            build_state_step(journal, backups),
            CredentialReferenceStep(journal=journal, backups=backups, refs_override={}),
            RuntimeTriageStep(journal=journal, backups=backups),
        ]
        pre_360_engine = MigrationEngine(pre_360_steps, data_home=config.DATA_HOME)
        apply_reports = pre_360_engine.apply()
        assert all(r.ok for r in apply_reports), [(r.step_id, r.summary) for r in apply_reports]
        assert layout_state() == "mixed"
        assert core_home() == config.RUNTIME_DIR / "core"  # step 8 hasn't run yet

        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"
        assert events == []  # no stale step, no rollback — the 8 old steps were untouched
        assert core_home() == config.DATA_HOME / "v2" / "system"  # the new step actually ran


class TestRunBootStageHappyPath:
    def test_v1_fixture_applies_and_lands_on_mixed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert layout_state() == "v1"

        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        progress: list[str] = []
        result = auto_migrate_boot.run_boot_stage(progress_cb=progress.append)

        assert result.action == "applied"
        assert layout_state() == "mixed"
        assert progress  # something was reported
        assert events and events[0]["event"] == "auto_migrate_applied"

        state = auto_migrate_boot.load_state()
        from agent_takkub import __version__ as app_version

        assert state == {"applied_version": app_version}


class TestRunBootStageRollback:
    def test_validate_failure_triggers_rollback_and_records_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert layout_state() == "v1"
        monkeypatch.setattr(
            MigrationEngine,
            "validate",
            lambda self: [StepReport("state", "validate", False, "fixture-forced-fail")],
        )
        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "rolled_back"
        assert layout_state() == "v1"  # rollback actually removed v2/
        assert events and events[0]["event"] == "auto_migrate_rolled_back"
        assert events[0]["failing_step"] == "state"

        from agent_takkub import __version__ as app_version

        assert auto_migrate_boot.load_state() == {"rolled_back_for_version": app_version}

    def test_apply_failure_also_triggers_rollback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            MigrationEngine,
            "apply",
            lambda self: [StepReport("readonly-registries", "apply", False, "boom")],
        )
        rollback_calls: list[str] = []
        real_rollback = MigrationEngine.rollback

        def _spy_rollback(self):
            rollback_calls.append("rollback")
            return real_rollback(self)

        monkeypatch.setattr(MigrationEngine, "rollback", _spy_rollback)

        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "rolled_back"
        assert rollback_calls == ["rollback"]

    def test_next_boot_after_rollback_is_skipped_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            MigrationEngine,
            "validate",
            lambda self: [StepReport("state", "validate", False, "fixture-forced-fail")],
        )
        first = auto_migrate_boot.run_boot_stage()
        assert first.action == "rolled_back"

        second = auto_migrate_boot.run_boot_stage()
        assert second.action == "skipped"
        assert second.reason == "previously-rolled-back"


# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------


class TestState:
    def test_missing_state_file_is_empty_dict(self) -> None:
        assert auto_migrate_boot.load_state() == {}

    def test_corrupt_state_file_is_empty_dict(self) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text("{not json", encoding="utf-8")
        assert auto_migrate_boot.load_state() == {}
