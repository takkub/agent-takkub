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

    def test_env_zero_disables_even_though_the_flag_is_always_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#515 Settings diet: there is no more Settings toggle to disable
        this with (`core_v2_settings.flag_enabled` is always True) — the env
        var is the ONLY escape hatch left, and it must still work."""
        monkeypatch.setenv("TAKKUB_AUTO_MIGRATE", "0")
        assert auto_migrate_boot.auto_migrate_enabled() is False

    def test_enabled_by_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_AUTO_MIGRATE", raising=False)
        assert auto_migrate_boot.auto_migrate_enabled() is True


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

    def test_sums_runtime_dir_file_sizes(self, tmp_path: Path) -> None:
        # Own data_home, not the shared `config.DATA_HOME`: a late
        # `_log_boot_event` / events.log flush from a sibling test landed
        # under runtime/ there and turned 150 into 592 on macOS CI (v1.6.3).
        data_home = tmp_path / "dh"
        runtime = data_home / "runtime" / "sessions"
        runtime.mkdir(parents=True)
        (runtime / "a.txt").write_bytes(b"x" * 100)
        (runtime / "b.txt").write_bytes(b"y" * 50)
        # #574: `pre-migrate-backup` copies this SAME content a second
        # time before promote/archive do — the estimate doubles the base
        # walk to account for it (see `_estimate_copy_bytes`'s own
        # docstring).
        assert auto_migrate_boot._estimate_copy_bytes(data_home) == 300

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

    def test_v2_layout_state_still_runs_apply_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504: `"v2"` is the normal steady state of every fully-migrated
        machine now, not a dead/unreachable branch — it must keep running
        `apply_pending()` on every boot forever, exactly like `"mixed"`
        does, so a ladder step added in a LATER release still gets picked
        up (#362's whole point) instead of every promoted machine being
        permanently stranded on whatever ladder existed the boot it first
        reached `"v2"`."""
        # `layout_state` is imported inside `run_boot_stage` — patch the
        # source module's binding, not a module-level name in this one.
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        result = auto_migrate_boot.run_boot_stage()
        assert result.action == "pending_applied"

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

    def test_cleanup_pending_step_failure_is_neither_rolled_back_nor_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#574 round9 G11 `boot_completes_after_a_transient_prune_denial`:
        a step whose failure detail names `cleanup_pending` (a transient
        prune denial — every file this attempt already copy-verified STAYS
        at its new home, only the source removal is outstanding) must be
        left alone — no `rollback_step()` call, and never added to
        `rolled_back_steps` — so the very next boot retries it
        unconditionally instead of being version-gated away forever."""
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "mixed")
        monkeypatch.setattr(MigrationEngine, "applied_step_ids", lambda self: ["state"])
        monkeypatch.setattr(
            MigrationEngine,
            "apply_pending",
            lambda self, **kw: [
                StepReport(
                    "promote-v2-root",
                    "apply",
                    False,
                    "cleanup-pending: prune denied",
                    detail={"cleanup_pending": ["state"]},
                )
            ],
        )
        rollback_calls: list[str] = []
        monkeypatch.setattr(
            MigrationEngine,
            "rollback_step",
            lambda self, step_id: (
                rollback_calls.append(step_id) or StepReport(step_id, "rollback", True, "restored")
            ),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "cleanup_pending"
        assert rollback_calls == []
        assert auto_migrate_boot.load_state().get("rolled_back_steps", {}) == {}

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
        steps), with real V1 data still on disk (`projects.json` — the
        fixture below), must, on its next mixed-state boot after upgrading
        to a build carrying #360's core-internal-store (and #504's
        promote/archive pair), apply just the steps genuinely new to this
        machine's journal — the other 8 stay untouched, no stale/rollback
        noise at all."""
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

        # Real V1 data still on disk — what actually makes this "mixed"
        # under #504's layout_state() (`projects.json` is one of the exact
        # markers it checks for).
        (config.DATA_HOME / "projects.json").write_text(
            json.dumps({"active": None, "projects": {}}), encoding="utf-8"
        )

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
        assert core_home() == config.DATA_HOME / "system"  # the new step actually ran

    def test_stale_routing_global_self_heals_within_the_same_promote_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Release-blocker repro (2026-09-07, prod on 2.0.0), reproduced on a
        simulated already-mixed 2.0.x machine: `role-agent` applied once
        under the PRE-#515 ladder (its nested `v2/routing.json`'s "global"
        mirrored the then-empty `role-providers.json`), and real per-role
        assignments were later saved into `role-models.json` by hand
        WITHOUT going through `role_models._save()`'s post-#515 `dual_write_
        routing` call — the mirror is stale: "global" still `{}` while
        `role-models.json` (the one V1 source of truth, #515) has real
        entries.

        #504: this machine's FIRST 2.1.0 boot must self-heal `role-agent`'s
        stale mirror BEFORE `archive-v1-legacy` (last in the ladder) sweeps
        `role-models.json` into the archive — ladder order, not a second
        boot, is what guarantees this now, since V1 stops being readable
        for this step the instant `archive-v1-legacy` succeeds (#504's own
        `_ARCHIVED_SOURCE_STEP_IDS` — see `engine.py`)."""
        from agent_takkub.core.migration.journal import MigrationJournal
        from agent_takkub.core.migration.registry_copy_step import write_json_atomic
        from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep
        from agent_takkub.core.storage.legacy_reader import read_json

        data_home = config.DATA_HOME
        monkeypatch.setattr(config, "SETTINGS_HOME", data_home)

        role_models = {
            "backend": {"provider": "claude"},
            "critic": {"provider": "codex"},
            "devops": {"provider": "claude"},
            "frontend": {"provider": "claude"},
            "lead": {"provider": "claude"},
            "mobile": {"provider": "claude"},
            "qa": {"provider": "gemini"},
            "reviewer": {"provider": "codex"},
        }
        (data_home / "role-models.json").write_text(json.dumps(role_models), encoding="utf-8")

        # Pre-existing 2.0.x nested v2/ root — `role-agent` already applied
        # once, its "global" mirror now stale (empty) relative to the real
        # role-models.json above (the #515 drift shape).
        routing_target = data_home / "v2" / "config" / "routing.json"
        write_json_atomic(routing_target, {"schema": 1, "global": {}, "projects": {}})
        journal = MigrationJournal()
        for step_id in (
            "version-marker",
            "readonly-registries",
            "role-agent",
            "capability",
            "project",
            "state",
            "credential-reference",
            "runtime-triage",
            "core-internal-store",
        ):
            journal.record(step_id, "apply", True, "seeded: pre-#504 machine already migrated once")

        assert layout_state() == "mixed"
        # Direct, not through the full engine's `validate()` — that stops
        # the line at `promote-v2-root` (legitimately not-yet-done on a
        # still-"mixed" machine) before ever reaching `role-agent`.
        assert (
            RoleAgentMigrationStep(data_home=data_home).validate().ok is False
        )  # confirms the repro

        events: list[dict] = []
        monkeypatch.setattr(
            auto_migrate_boot,
            "_log_boot_event",
            lambda ev, **kw: events.append({"event": ev, **kw}),
        )

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"
        assert events == []  # no stale-step bookkeeping, no rollback event at all
        assert auto_migrate_boot.load_state().get("stale_applied_steps", {}) == {}
        assert auto_migrate_boot.load_state().get("rolled_back_steps", {}) == {}

        # Promoted to the top level, self-healed, THEN archived — all in
        # this one boot.
        assert layout_state() == "v2"
        promoted_routing = RoleAgentMigrationStep(data_home=data_home)._routing_target()
        healed = read_json(promoted_routing)
        assert healed["global"] == {role: e["provider"] for role, e in role_models.items()}
        assert not (data_home / "role-models.json").exists()  # archived by archive-v1-legacy

        final_reports = MigrationEngine(data_home=data_home).validate()
        assert all(r.ok for r in final_reports), [(r.step_id, r.summary) for r in final_reports]


class TestRunBootStageHappyPath:
    def test_v1_fixture_applies_and_lands_on_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#504: a genuinely fresh/empty fixture has no V1 leftovers for
        `ArchiveV1LegacyStep` to archive, so a first-ever full-ladder apply
        lands straight on `"v2"` — no more permanent `"mixed"` limbo for a
        box with nothing to migrate."""
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
        assert layout_state() == "v2"
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


# ---------------------------------------------------------------------------
# #504 — boot-time "finish the move": a simulated OLD machine (real
# installed-build shape: SETTINGS_HOME == DATA_HOME, a pre-existing populated
# nested v2/ root from 2.0.x, plus genuine V1 top-level leftovers) boots
# 2.1.0 and the archive+promote pair runs automatically via run_boot_stage()
# — verified end to end, not by hand-invoking the steps.
# ---------------------------------------------------------------------------


def _build_old_machine(data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real installed-build shape: SETTINGS_HOME == DATA_HOME (#504's own
    scope only ever runs on that shape — `is_dev_checkout()` excludes every
    other one), a pre-existing 2.0.x nested v2/ root, real V1 leftovers, and
    the exact never-touch infrastructure #504 item 8 names — all under one
    `data_home` so the test can assert every one of them survives.

    Also seeds the REAL migration journal with "already applied" records for
    the 9 pre-#504 ladder steps — a real 2.0.x machine's journal has these
    (that's what running the ladder once already wrote), and `apply_pending()`
    needs them to know those 9 are done and only `promote-v2-root`/
    `archive-v1-legacy` are genuinely new — without this, it would re-run
    every domain step fresh from (in this fixture, empty) V1 sources and
    clobber the nested v2/ content this function just placed."""
    monkeypatch.setattr(config, "SETTINGS_HOME", data_home)

    from agent_takkub.core.migration.journal import MigrationJournal

    journal = MigrationJournal()
    for step_id in (
        "version-marker",
        "readonly-registries",
        "role-agent",
        "capability",
        "project",
        "state",
        "credential-reference",
        "runtime-triage",
        "core-internal-store",
    ):
        journal.record(step_id, "apply", True, "seeded: pre-#504 machine already migrated once")

    # V1 source for `readonly-registries`'s "provider-models" mapping — a
    # real already-migrated machine still has this (pre-#504 never deleted
    # V1), consistent with the nested v2/ mirror below (otherwise
    # `apply_pending()` would correctly see the mirror as stale and
    # legitimately re-derive it from V1 — which would just rewrite the SAME
    # content here, but the test below wants to prove the PROMOTE path
    # specifically, not a coincidental re-derive).
    (data_home / "provider-models.json").write_text(
        json.dumps({"claude": "claude-sonnet-5"}), encoding="utf-8"
    )

    # Pre-existing 2.0.x nested v2/ root (already fully migrated once) —
    # `readonly-registries`'s own wrapped shape, matching the V1 source above.
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "registry.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "migrated_from": "seed",
                "migrated_at": 0,
                "data": {"claude": "claude-sonnet-5"},
            }
        ),
        encoding="utf-8",
    )
    (data_home / "v2" / "config" / "features").mkdir(parents=True)
    (data_home / "v2" / "config" / "features" / "rtk.json").write_text(
        json.dumps({"schema": 1, "migrated_from": "seed", "migrated_at": 0, "data": {}}),
        encoding="utf-8",
    )

    # Real V1 top-level leftovers still on disk.
    (data_home / "projects.json").write_text(
        json.dumps({"active": None, "projects": {"demo": {}}}), encoding="utf-8"
    )
    (data_home / "custom-roles.json").write_text("{}", encoding="utf-8")

    # #504 item 8 "ไม่แตะ" — never archived, never promoted over.
    (data_home / "runtime" / "core").mkdir(parents=True, exist_ok=True)
    (data_home / "runtime" / "core" / "version.json").write_text("{}", encoding="utf-8")
    (data_home / "worktrees" / "demo" / "wt-1").mkdir(parents=True)
    (data_home / "worktrees" / "demo" / "wt-1" / "code.py").write_text("x = 1", encoding="utf-8")
    (data_home / "claude-config").mkdir(parents=True)
    (data_home / "claude-config" / "auth.json").write_text("secret", encoding="utf-8")

    # #504 item 5 — deleted outright, not archived.
    (data_home / "openviking").mkdir(parents=True)
    (data_home / "openviking" / "stale.json").write_text("{}", encoding="utf-8")


class TestPromoteBootIntegration:
    def test_old_machine_boot_promotes_and_archives_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_home = config.DATA_HOME
        _build_old_machine(data_home, monkeypatch)
        # Explicit per the task's own instruction, even though a tmp
        # data_home already makes this False — belt and suspenders against
        # this test ever silently running against a real dev checkout.
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)

        assert layout_state(data_home) == "mixed"

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_applied"
        assert layout_state(data_home) == "v2"

        # Promoted: the pre-existing nested v2/ content is now top-level.
        assert not (data_home / "v2").exists()
        assert json.loads((data_home / "models" / "registry.json").read_text())["data"] == {
            "claude": "claude-sonnet-5"
        }
        assert (data_home / "config" / "features" / "rtk.json").exists()

        # Archived: V1 leftovers moved into backups/v1-archive-<ts>/, never deleted.
        assert not (data_home / "projects.json").exists()
        assert not (data_home / "custom-roles.json").exists()
        archive_dirs = [
            p for p in (data_home / "backups").iterdir() if p.name.startswith("v1-archive-")
        ]
        assert len(archive_dirs) == 1
        archived_projects = json.loads((archive_dirs[0] / "projects.json").read_text())
        assert archived_projects == {"active": None, "projects": {"demo": {}}}

        # Deleted outright — #504 item 5, gone for good.
        assert not (data_home / "openviking").exists()

        # Never touched.
        assert (data_home / "runtime" / "core" / "version.json").exists()
        assert (data_home / "worktrees" / "demo" / "wt-1" / "code.py").read_text() == "x = 1"
        assert (data_home / "claude-config" / "auth.json").read_text() == "secret"

    def test_fresh_data_home_boot_gets_the_new_layout_with_no_archive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504 item 4 — a genuinely empty DATA_HOME gets the promoted
        layout straight away, no `v2` name anywhere, no floating json, no
        archive folder at all."""
        data_home = config.DATA_HOME
        monkeypatch.setattr(config, "SETTINGS_HOME", data_home)
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "applied"
        assert layout_state(data_home) == "v2"
        assert not (data_home / "v2").exists()
        assert not (data_home / "backups").exists()


class TestPromoteBootFailureHandling:
    """2026-09-10 acceptance review (docs/audit/2026-09-10-504-acceptance-
    review.md) B2/H4/H6/H8 — reproduced against the real `_build_old_machine`
    fixture and `run_boot_stage()`, not by hand-invoking individual steps."""

    def test_promote_failure_during_mixed_boot_never_loses_the_unique_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504 B2 `pending_failure` repro: inject a failure on the FIRST
        `promote-v2-root` copy during a mixed-state boot. The unique nested
        content must survive SOMEWHERE, and `archive-v1-legacy` must never
        have run in this same pass at all (it used to find the still-full
        `v2/` and `shutil.rmtree` it outright)."""
        data_home = config.DATA_HOME
        _build_old_machine(data_home, monkeypatch)
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)
        (data_home / "v2" / "state").mkdir(parents=True)
        (data_home / "v2" / "state" / "only-copy.json").write_text(
            "unique-v2-data", encoding="utf-8"
        )

        import agent_takkub.core.migration.promote_v1 as promote_mod

        real_copy_verified = promote_mod.copy_verified

        def _fail_promote(src, dest):
            if src.parent == data_home / "v2":
                raise OSError("injected promote failure")
            return real_copy_verified(src, dest)

        monkeypatch.setattr(promote_mod, "copy_verified", _fail_promote)

        result = auto_migrate_boot.run_boot_stage()

        assert result.action == "pending_rolled_back"
        assert list(data_home.rglob("only-copy.json"))
        assert not (data_home / "state" / "only-copy.json").exists()
        # `archive-v1-legacy` must never have run in this same pass — #574's
        # `pre-migrate-backup` legitimately DOES create `backups/pre-migrate-
        # <ts>/` on this fixture (it has real V1 content), so check for the
        # ABSENCE of an archive generation specifically, not of `backups/`
        # itself.
        assert not list(data_home.glob("backups/v1-archive-*"))

    def test_disk_gate_runs_on_the_mixed_pending_path_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504 H4 `disk_gate` repro: the "v2"/"mixed" branch used to jump
        straight to `apply_pending()` without ever consulting
        `_disk_has_room` — patched to always fail, it used to be called
        zero times and the migration ran anyway."""
        data_home = config.DATA_HOME
        _build_old_machine(data_home, monkeypatch)
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)
        calls = {"n": 0}
        real_disk_has_room = auto_migrate_boot._disk_has_room

        def _tracked(home):
            calls["n"] += 1
            return False

        monkeypatch.setattr(auto_migrate_boot, "_disk_has_room", _tracked)

        result = auto_migrate_boot.run_boot_stage()

        assert calls["n"] > 0
        assert result.action == "skipped"
        assert result.reason == "disk-space"
        assert real_disk_has_room  # sanity: original still importable/callable

    def test_disk_estimate_counts_a_populated_nested_v2_root(self) -> None:
        """#504 H4: a populated `v2/` with no `runtime/` at all used to
        estimate exactly zero bytes, passing any disk gate unconditionally."""
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "large.bin").write_bytes(b"x" * 4096)
        # #574: doubled — `pre-migrate-backup` copies this content too.
        assert auto_migrate_boot._estimate_copy_bytes(data_home) == 4096 * 2

    def test_disk_estimate_counts_archive_only_candidates(self) -> None:
        """#504 R2-H3 `disk_archive_inventory`: a fixture with ONLY a V1
        top-level leftover (no `runtime/`, no nested `v2/`) used to estimate
        exactly zero bytes too — `ArchiveV1LegacyStep`'s own candidates
        weren't counted at all."""
        data_home = config.DATA_HOME
        (data_home / "unmapped-legacy").mkdir(parents=True)
        (data_home / "unmapped-legacy" / "large.bin").write_bytes(b"x" * 8192)
        # #574: doubled — `pre-migrate-backup` copies this content too.
        assert auto_migrate_boot._estimate_copy_bytes(data_home) == 8192 * 2
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                auto_migrate_boot.shutil,
                "disk_usage",
                lambda _path: type("Usage", (), {"free": 0})(),
            )
            assert auto_migrate_boot._disk_has_room(data_home) is False

    def test_disk_estimate_counts_a_promote_merge_preimage(self) -> None:
        """#504 R2-H3: a promote candidate that will MERGE into an
        already-populated top-level destination must count that existing
        destination too — `_two_phase_move` backs it up before merging, so
        room must cover the preimage, not just the new copy."""
        data_home = config.DATA_HOME
        (data_home / "v2" / "providers" / "claude").mkdir(parents=True)
        (data_home / "v2" / "providers" / "claude" / "new.json").write_bytes(b"n" * 10)
        (data_home / "providers" / "kimi" / "default").mkdir(parents=True)
        (data_home / "providers" / "kimi" / "default" / "auth.json").write_bytes(b"k" * 2000)
        # The nested v2/ dir itself already counts the new bytes; the
        # pre-existing top-level "providers" destination's own bytes must
        # be counted ON TOP of that.
        assert auto_migrate_boot._estimate_copy_bytes(data_home) >= 10 + 2000

    def test_version_bump_on_first_upgraded_boot_still_validates_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504 H6 `version_upgrade` repro: `version-marker` runs before
        `promote-v2-root` can have materialized the post-#504 `system/`
        location for the first time — its fresh stamp used to get shadowed
        by promote copying an OLD nested `v2/system/version.json` over it,
        so the very first upgraded boot validated red."""
        import agent_takkub
        from agent_takkub.core.migration import steps as steps_mod
        from agent_takkub.core.migration.engine import MigrationEngine
        from agent_takkub.core.versioning.store import record_component

        data_home = config.DATA_HOME
        _build_old_machine(data_home, monkeypatch)
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)
        # An established old-version marker under the pre-promotion nested
        # location — as if this machine ran 2.0.8's ladder before upgrading.
        record_component("app", "2.0.8", path=data_home / "v2" / "system" / "version.json")
        # The actual real upgrade this boot represents: running build moved
        # past 2.0.8. `steps.APP_VERSION` is bound at import time
        # (`from agent_takkub import __version__ as APP_VERSION`), so it
        # needs patching directly — patching `agent_takkub.__version__`
        # alone wouldn't reach that already-bound name.
        monkeypatch.setattr(steps_mod, "APP_VERSION", "2.1.0")
        monkeypatch.setattr(agent_takkub, "__version__", "2.1.0")

        result = auto_migrate_boot.run_boot_stage()
        validation = MigrationEngine().validate()

        assert result.action == "pending_applied"
        version_marker_reports = [r for r in validation if r.step_id == "version-marker"]
        assert version_marker_reports and version_marker_reports[0].ok, [
            (r.step_id, r.summary) for r in validation
        ]

    def test_fresh_boot_state_file_does_not_get_archived_on_the_second_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#504 H8 `fresh_two_boots` repro: a genuinely fresh DATA_HOME's
        first boot used to write its own bookkeeping file
        (`auto-migrate-state.json`) at the top level, which the very NEXT
        boot then archived as an unrecognized V1 leftover — a machine that
        never had any V1 data still got a brand-new v1-archive."""
        data_home = config.DATA_HOME
        monkeypatch.setattr(config, "SETTINGS_HOME", data_home)
        monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)

        first = auto_migrate_boot.run_boot_stage()
        assert first.action == "applied"
        assert not (data_home / "backups").exists()

        second = auto_migrate_boot.run_boot_stage()
        assert second.action == "pending_applied"
        assert not (data_home / "backups").exists()


class TestMigrateCliRestoreV1:
    """`takkub migrate apply` / `validate` / `restore-v1` driven directly
    through the CLI (`agent_takkub.cli.main`), not by hand-invoking the
    step classes — per #504's own "ทดสอบผ่าน takkub migrate apply/validate/
    restore-v1 ตรงๆ" requirement."""

    def test_apply_validate_restore_v1_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub import cli

        data_home = config.DATA_HOME
        _build_old_machine(data_home, monkeypatch)

        rc = cli.main(["migrate", "apply", "--json"])
        assert rc == 0
        assert layout_state(data_home) == "v2"
        assert (data_home / "models" / "registry.json").exists()

        rc = cli.main(["migrate", "validate", "--json"])
        assert rc == 0

        rc = cli.main(["migrate", "restore-v1", "--json"])
        assert rc == 0

        # Back to the exact pre-2.1.0 shape.
        assert (data_home / "projects.json").exists()
        assert json.loads((data_home / "projects.json").read_text()) == {
            "active": None,
            "projects": {"demo": {}},
        }
        assert json.loads((data_home / "v2" / "models" / "registry.json").read_text())["data"] == {
            "claude": "claude-sonnet-5"
        }
        assert layout_state(data_home) == "mixed"

        # The archive itself is untouched by restore — never expires.
        archive_dirs = [
            p for p in (data_home / "backups").iterdir() if p.name.startswith("v1-archive-")
        ]
        assert len(archive_dirs) == 1
        assert (archive_dirs[0] / "projects.json").exists()
