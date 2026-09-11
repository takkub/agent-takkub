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
    # #504/#574 R8-L1 (= R4-M4): `_choice_path()` resolves via
    # `effective_data_home(None, prefer_primary=True)` -> `_primary_data_home()`
    # — deliberately NOT `config.DATA_HOME` (see `effective_data_home`'s own
    # docstring: resolving `None` there would silently bypass a test's
    # `config.DATA_HOME` patch) — so the module-level `_isolate_paths`
    # autouse fixture above does not reach it. Without this, these tests
    # read/write the REAL primary cockpit's `v2/config/boot-provider-
    # choice.json` — across every worktree pane on the machine, not just
    # this test process — making `test_no_choice_yet_returns_none` fail
    # whenever an earlier test/pane already wrote a real choice there and
    # pass only in isolation. Patch `_choice_path` itself, straight to a
    # `tmp_path` file, so every test here is fully isolated regardless of
    # real machine state.
    @pytest.fixture(autouse=True)
    def _isolate_choice_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            boot_flow, "_choice_path", lambda: tmp_path / "boot-provider-choice.json"
        )

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

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
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

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("promote-v2-root", "models")
            progress_cb("x")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        def _raise(_event):
            raise RuntimeError("ui crashed")

        outcome = boot_flow.run_migration(progress_cb=_raise)
        assert outcome.ok

    def test_on_entry_repeat_notify_from_prune_phase_never_pushes_done_past_total(
        self, monkeypatch
    ):
        """#574 round12 item 2: `promote-v2-root`/`archive-v1-legacy` each
        fire `on_entry(step_id, name)` TWICE per top-level entry over a full
        `apply()` — once from their own copy phase, once from their
        deferred prune phase (`_copy_phase`/`_prune_phase` share the same
        bound `on_entry`). A real prod rehearsal observed `done` climb to
        exactly 2x `total` (9->18, 27->54) because the old code counted
        every notify as new progress. The SAME name notified again must
        never advance `done` past `total`, and `percent_overall` must never
        exceed 100."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("promote-v2-root", "models")  # copy phase
            on_entry("promote-v2-root", "models")  # deferred prune phase, same entry
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)
        entry_events = [e for e in events if e.log_line == "promote-v2-root: models"]
        assert len(entry_events) == 2
        assert entry_events[0].done == entry_events[1].done == entry_events[0].total == 1
        assert all(e.done <= e.total for e in events if e.done is not None and e.total)
        assert all(0.0 <= e.percent_overall <= 100.0 for e in events)

    def test_domain_step_on_step_emits_phase_3_start_and_done(self, monkeypatch):
        """#574 round12 item 3: the 8 domain steps produced ZERO progress
        events before this fix — `MigrationEngine`'s new `on_step` observer
        gives each a start/done pair, positioned by its fixed ladder index
        out of the real ladder length (`plan.verify_steps`), so a wizard
        watching for phase 3 no longer jumps straight from 2 to 4."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_step("readonly-registries", "start")
            on_step("readonly-registries", "done")
            on_step("role-agent", "start")
            on_step("role-agent", "done")
            on_step("promote-v2-root", "start")  # not a domain step -> ignored
            on_step("promote-v2-root", "done")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        outcome = boot_flow.run_migration(progress_cb=events.append)
        phase3 = [e for e in events if e.phase == 3]
        # readonly-registries is ladder position 4, role-agent is position 5
        # (pre-migrate-backup, version-marker, promote-v2-root come first).
        assert [e.done for e in phase3] == [3, 4, 4, 5]
        assert all(e.total == 12 for e in phase3)  # plan.verify_steps, full default ladder
        assert all(e.unit == "ขั้น" for e in phase3)
        assert [e.log_operation for e in phase3] == [
            "readonly-registries",
            "readonly-registries",
            "role-agent",
            "role-agent",
        ]
        assert outcome.ok

    def test_eta_s_stays_none_until_enough_signal_in_the_current_phase(self, monkeypatch):
        """#574 round12 item 4 (R3-M3): eta is computed from the CURRENT
        phase's own done/elapsed rate, only once there is enough signal to
        trust it (>= 2s elapsed in that phase) — before that it stays
        `None` rather than reporting noise from a single early event."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        for name in ("a", "b", "c", "d", "e"):
            (data_home / "v2" / name).mkdir(parents=True)
            (data_home / "v2" / name / "f.json").write_text("{}", encoding="utf-8")

        clock = {"t": 1000.0}
        monkeypatch.setattr(boot_flow.time, "monotonic", lambda: clock["t"])

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("promote-v2-root", "a")  # phase 1->2 transition resets the phase clock
            clock["t"] += 0.5
            on_entry("promote-v2-root", "b")
            clock["t"] += 3.0
            on_entry("promote-v2-root", "c")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)
        phase2 = [e for e in events if e.phase == 2 and e.current_path in ("a", "b", "c")]
        assert [e.current_path for e in phase2] == ["a", "b", "c"]
        assert phase2[0].eta_s is None  # 0s elapsed in this phase
        assert phase2[1].eta_s is None  # 0.5s elapsed, still under the 2s/5-event gate
        assert phase2[2].eta_s is not None and phase2[2].eta_s > 0  # 3.5s elapsed -> populated

    def test_percent_and_phase_never_regress_on_a_late_out_of_order_notify(self, monkeypatch):
        """#504/#574 R8-M1: `promote-v2-root`'s deferred prune fires its own
        `on_entry` again (see the round12-item-2 test above) — sometimes
        AFTER `archive-v1-legacy` has already advanced the stream to phase
        4 (a real prod rehearsal observed `percent_overall` fall from 99.0
        to 72.73 and `phase` fall from 4 to 2 at exactly this point). Both
        must hold to their highest value ever reached, never walk
        backwards for a late notify belonging to an earlier phase."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")
        (data_home / "legacyfile.json").write_text("legacy", encoding="utf-8")

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("archive-v1-legacy", "legacyfile.json")  # advances the stream to phase 4
            on_entry("promote-v2-root", "models")  # late, out-of-order deferred-prune repeat
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)
        phases = [e.phase for e in events]
        percents = [e.percent_overall for e in events]
        assert phases == sorted(phases)  # never decreases across the whole stream
        assert percents == sorted(percents)  # ditto for percent_overall
        assert phases[-1] >= 4  # the late phase-2 notify never dragged it back down

    def test_a_real_done_past_the_plans_own_total_grows_the_total_instead_of_clamping(
        self, monkeypatch
    ):
        """#504/#574 R8-M4: `archive-v1-legacy` genuinely processing more
        top-level entries than `plan.archive_items` estimated (a real
        plan/runtime mismatch, distinct from the round12-item-2 double-
        notify a dedup already handles) used to be silently clamped down
        to the plan's own total, permanently sticking the counter at
        100% instead of surfacing the gap. The total must grow to match
        what actually ran, and the mismatch must be noted in `log_detail`."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "legacyfile.json").write_text("legacy", encoding="utf-8")  # plan sees 1

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("archive-v1-legacy", "legacyfile.json")
            on_entry("archive-v1-legacy", "another-leftover.json")  # 2nd distinct name > plan's 1
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)
        phase4 = [e for e in events if e.phase == 4 and e.log_operation == "archive-v1-legacy"]
        assert [e.done for e in phase4] == [1, 2]
        # total grew to match the 2nd, real event instead of clamping it to 1
        assert phase4[1].total == 2
        assert "undercounted" in phase4[1].log_detail

    def test_the_opening_and_closing_info_events_carry_log_detail(self, monkeypatch):
        """#504/#574 R4-H2: the interface contract (docs/v2/574-boot-flow-
        interface.md "log structure") requires `log_detail` to carry the
        full message text for every "info"/"validate" event — the two
        events that bracket the whole migration used to leave it empty, so
        a window reading the structured fields (never re-parsing
        `log_line`) rendered only "HH:MM:SS  info" with the message lost."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        monkeypatch.setattr(
            auto_migrate_boot,
            "run_boot_stage",
            lambda *a, **k: auto_migrate_boot.BootMigrationResult("applied", messages=[]),
        )
        events = []
        boot_flow.run_migration(progress_cb=events.append)
        info_events = [e for e in events if e.log_operation == "info"]
        assert len(info_events) == 2  # opening (phase 1) + closing (phase 5)
        assert all(e.log_line and e.log_detail == e.log_line for e in info_events)

    def test_entry_counter_unit_stays_constant_within_a_phase(self, monkeypatch):
        """#504/#574 R4-H1 (supersedes round12 item 6/R3-M4): `unit` used to
        be derived from the CURRENT entry's own name (`_entry_unit`) even
        though `done`/`total` count TOP-LEVEL ENTRIES, not that entry's
        content-type — one row's noun flipped mid-phase as different-typed
        entries streamed through ("1/4 ไฟล์" -> "2/4 โปรเจค") with no change
        in what the number itself meant. `unit` must describe `done`/
        `total` themselves: always "รายการ" for the entry-level counter
        (phase 1/2/4, both `on_entry` and `on_file_progress`), matching the
        mockup's own wording (V9). Phase 3's `on_step` counter still uses
        its own distinct "ขั้น" (a step count, never entry-typed)."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("promote-v2-root", "projects")
            on_entry("promote-v2-root", "models")
            on_file_progress("promote-v2-root", "models", 1, 2, "registry.json")
            on_step("readonly-registries", "start")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)
        entry_counter_events = [e for e in events if e.log_operation == "promote-v2-root"]
        assert len(entry_counter_events) == 3  # 2 on_entry + 1 on_file_progress
        assert all(e.unit == "รายการ" for e in entry_counter_events)
        phase3_events = [e for e in events if e.phase == 3]
        assert phase3_events and all(e.unit == "ขั้น" for e in phase3_events)

    def test_log_line_carries_structured_operation_detail_and_timestamp(self, monkeypatch):
        """#574 round12 item 7 (R3-M5): a UI must never re-parse `log_line`
        itself — every event now also carries `log_operation` (a machine
        token: a step_id, or "validate"/"info" for a plain text message),
        `log_detail` (the free-form explanation), and `log_timestamp`
        (`HH:MM:SS`); `current_path` (pre-existing) is the 4th part."""
        import re

        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        (data_home / "v2" / "models").mkdir(parents=True)
        (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")

        def fake_run_boot_stage(
            *, progress_cb=None, on_entry=None, on_file_progress=None, on_step=None
        ):
            on_entry("promote-v2-root", "models")
            progress_cb("apply สำเร็จ — กำลัง validate…")
            progress_cb("บางข้อความทั่วไป")
            return auto_migrate_boot.BootMigrationResult("applied", messages=[])

        monkeypatch.setattr(auto_migrate_boot, "run_boot_stage", fake_run_boot_stage)

        events = []
        boot_flow.run_migration(progress_cb=events.append)

        entry_event = next(e for e in events if e.current_path == "models")
        assert entry_event.log_operation == "promote-v2-root"
        assert entry_event.log_timestamp and re.fullmatch(
            r"\d{2}:\d{2}:\d{2}", entry_event.log_timestamp
        )

        validate_event = next(e for e in events if e.log_detail == "apply สำเร็จ — กำลัง validate…")
        assert validate_event.log_operation == "validate"
        assert validate_event.phase == 3

        info_event = next(e for e in events if e.log_detail == "บางข้อความทั่วไป")
        assert info_event.log_operation == "info"

    def test_previous_version_captured_from_version_json_before_this_runs_apply(self, monkeypatch):
        """#574 round12 item 8 (R3-M6): the "app" component `version.json`
        holds BEFORE this call's own version-marker overwrite — read here
        so screen D's downgrade note can say what `restore-v1` would put
        back, and screen E can contrast it with the currently running
        build."""
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot
        from agent_takkub.core.versioning.store import record_component

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        record_component("app", "2.0.8")
        monkeypatch.setattr(
            auto_migrate_boot,
            "run_boot_stage",
            lambda **k: auto_migrate_boot.BootMigrationResult("skipped", messages=[]),
        )

        outcome = boot_flow.run_migration()
        assert outcome.previous_version == "2.0.8"

    def test_previous_version_is_none_on_a_from_scratch_install(self, monkeypatch):
        import agent_takkub.core.storage.layout as layout_mod
        from agent_takkub import auto_migrate_boot

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
        monkeypatch.setattr(
            auto_migrate_boot,
            "run_boot_stage",
            lambda **k: auto_migrate_boot.BootMigrationResult("skipped", messages=[]),
        )

        outcome = boot_flow.run_migration()
        assert outcome.previous_version is None

    def test_files_progress_reports_nonzero_files_total_for_a_thousand_file_entry(
        self, monkeypatch
    ):
        """#574 round12 item 5: verify `on_file` from `verify_copy.py` is
        genuinely wired through to phase-2 `ProgressEvent`s on a real (not
        mocked) migration run over a large entry — a real production
        rehearsal's own claim that files_done/files_total stayed null needs
        an end-to-end check, not just unit coverage of the plumbing."""
        import agent_takkub.core.storage.layout as layout_mod

        monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v1")
        data_home = config.DATA_HOME
        big_dir = data_home / "v2" / "providers"
        big_dir.mkdir(parents=True)
        for i in range(1000):
            (big_dir / f"file-{i}.json").write_text("{}", encoding="utf-8")

        events = []
        outcome = boot_flow.run_migration(progress_cb=events.append)
        assert outcome.ok
        files_events = [e for e in events if e.files_total]
        assert files_events, "expected at least one progress event with files_total > 0"
        assert any(e.files_total == 1000 for e in files_events)
        assert any(
            e.files_done == e.files_total == 1000 for e in files_events
        )  # guaranteed final call
