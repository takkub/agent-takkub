"""Core V2 migration engine: journal, copy-never-move backups, the
VersionMarkerStep proof-of-pipeline step, and MigrationEngine's
stop-the-line semantics (#309 Phase 4)."""

from __future__ import annotations

import json
import shutil

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.engine import MigrationEngine
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.report import StepReport
from agent_takkub.core.migration.steps import VersionMarkerStep
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.versioning import store as version_store

# ---------------------------------------------------------------------------
# journal.py
# ---------------------------------------------------------------------------


def test_journal_records_and_reads_entries(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    journal.record("step-a", "apply", True, "did the thing")
    entries = journal.all_entries()
    assert len(entries) == 1
    assert entries[0].step_id == "step-a"
    assert entries[0].action == "apply"
    assert entries[0].ok is True


def test_applied_step_ids_tracks_apply_then_rollback(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    journal.record("a", "apply", True)
    journal.record("b", "apply", True)
    assert journal.applied_step_ids() == ["a", "b"]

    journal.record("a", "rollback", True)
    assert journal.applied_step_ids() == ["b"]


def test_applied_step_ids_ignores_failed_apply(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    journal.record("a", "apply", False, "boom")
    assert journal.applied_step_ids() == []


# ---------------------------------------------------------------------------
# backup.py
# ---------------------------------------------------------------------------


def test_backup_of_missing_source_returns_none(tmp_path):
    mgr = BackupManager(tmp_path / "backups")
    result = mgr.backup("step-a", tmp_path / "does-not-exist.json")
    assert result is None


def test_backup_and_restore_round_trip(tmp_path):
    mgr = BackupManager(tmp_path / "backups")
    source = tmp_path / "data.json"
    source.write_text('{"v": 1}', encoding="utf-8")

    backup_path = mgr.backup("step-a", source)
    assert backup_path is not None
    assert backup_path.read_text(encoding="utf-8") == '{"v": 1}'
    assert source.exists()  # copy-never-move: source untouched

    source.write_text('{"v": 2}', encoding="utf-8")
    mgr.restore(backup_path, source)
    assert source.read_text(encoding="utf-8") == '{"v": 1}'
    assert backup_path.exists()  # restore doesn't delete the backup slot either


def test_latest_backup_returns_newest_slot(tmp_path):
    mgr = BackupManager(tmp_path / "backups")
    source = tmp_path / "data.json"
    source.write_text("v1", encoding="utf-8")
    mgr.backup("step-a", source)
    source.write_text("v2", encoding="utf-8")
    mgr.backup("step-a", source)

    latest = mgr.latest_backup("step-a", "data.json")
    assert latest is not None
    assert latest.read_text(encoding="utf-8") == "v2"


def test_latest_backup_missing_step_is_none(tmp_path):
    mgr = BackupManager(tmp_path / "backups")
    assert mgr.latest_backup("never-ran", "data.json") is None


# ---------------------------------------------------------------------------
# steps.VersionMarkerStep
# ---------------------------------------------------------------------------


def _fresh_step(tmp_path) -> tuple[VersionMarkerStep, MigrationJournal, BackupManager]:
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    step = VersionMarkerStep(journal=journal, backups=backups)
    return step, journal, backups


def test_version_marker_step_full_lifecycle_no_prior_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_takkub.core.migration.steps.version_doc_path", lambda: tmp_path / "version.json"
    )
    step, journal, _ = _fresh_step(tmp_path)

    inspect_report = step.inspect()
    assert inspect_report.ok is True
    assert "missing" in inspect_report.summary

    plan_report = step.plan()
    assert plan_report.ok is True

    dry_report = step.dry_run()
    assert dry_report.ok is True
    assert "would change" in dry_report.summary

    apply_report = step.apply()
    assert apply_report.ok is True
    assert (tmp_path / "version.json").exists()

    validate_report = step.validate()
    assert validate_report.ok is True

    rollback_report = step.rollback()
    assert rollback_report.ok is True
    assert not (tmp_path / "version.json").exists()

    actions = [(e.step_id, e.action, e.ok) for e in journal.all_entries()]
    assert ("version-marker", "apply", True) in actions
    assert ("version-marker", "rollback", True) in actions


def test_version_marker_step_rollback_restores_prior_value(tmp_path, monkeypatch):
    path = tmp_path / "version.json"
    monkeypatch.setattr("agent_takkub.core.migration.steps.version_doc_path", lambda: path)
    version_store.record_component("app", "0.0.1-prior", path=path)

    step, _journal, _backups = _fresh_step(tmp_path)
    step.apply()
    assert {c.component: c.version for c in version_store.read_version_doc(path)}[
        "app"
    ] != "0.0.1-prior"

    step.rollback()
    restored = {c.component: c.version for c in version_store.read_version_doc(path)}
    assert restored["app"] == "0.0.1-prior"


def test_version_marker_step_dry_run_never_writes(tmp_path, monkeypatch):
    path = tmp_path / "version.json"
    monkeypatch.setattr("agent_takkub.core.migration.steps.version_doc_path", lambda: path)
    step, _journal, _backups = _fresh_step(tmp_path)
    step.dry_run()
    assert not path.exists()


# ---------------------------------------------------------------------------
# engine.MigrationEngine
# ---------------------------------------------------------------------------


class _FakeStep:
    def __init__(self, step_id: str, ok: bool = True):
        self.step_id = step_id
        self.ok = ok
        self.calls: list[str] = []

    def inspect(self) -> StepReport:
        self.calls.append("inspect")
        return StepReport(self.step_id, "inspect", True, "ok")

    def plan(self) -> StepReport:
        self.calls.append("plan")
        return StepReport(self.step_id, "plan", True, "ok")

    def dry_run(self) -> StepReport:
        self.calls.append("dry_run")
        return StepReport(self.step_id, "dry_run", True, "ok")

    def apply(self) -> StepReport:
        self.calls.append("apply")
        return StepReport(self.step_id, "apply", self.ok, "ok" if self.ok else "failed")

    def validate(self) -> StepReport:
        self.calls.append("validate")
        return StepReport(self.step_id, "validate", self.ok, "ok" if self.ok else "failed")

    def rollback(self) -> StepReport:
        self.calls.append("rollback")
        return StepReport(self.step_id, "rollback", self.ok, "ok" if self.ok else "failed")


def test_engine_default_steps_starts_with_version_marker(tmp_path, monkeypatch):
    """The default ladder (#309 Phase 8b, plan §5.3, + #504's promote pair,
    + #574's pre-migrate-backup) is pre-migrate-backup + version-marker +
    promote-v2-root + the 7 V1->V2 steps + archive-v1-legacy, in risk order
    — pre-migrate-backup runs FIRST of all (a failed backup must abort the
    whole ladder before anything else is touched, achieved purely by ladder
    POSITION); version-marker predates the ladder and other code (doctor)
    depends on it running; promote-v2-root comes right after it (before any
    V1->V2 step's validate() runs in the same pass — see `core.migration
    .promote_v1`'s module docstring) and archive-v1-legacy stays last
    (every step above needs its V1 source still on disk to read from)."""
    monkeypatch.setattr(
        "agent_takkub.core.migration.steps.version_doc_path", lambda: tmp_path / "version.json"
    )
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path / "settings_home")
    engine = MigrationEngine()
    reports = engine.inspect()
    assert len(reports) == 12
    assert engine.step_count() == 12  # #504/#574 round10: boot_flow's own preview reads this
    assert reports[0].step_id == "pre-migrate-backup"
    assert reports[1].step_id == "version-marker"
    assert [r.step_id for r in reports[2:]] == [
        "promote-v2-root",
        "readonly-registries",
        "role-agent",
        "capability",
        "project",
        "state",
        "credential-reference",
        "runtime-triage",
        "core-internal-store",
        "archive-v1-legacy",
    ]
    assert all(r.ok for r in reports)


def test_engine_apply_stops_on_first_failure():
    a = _FakeStep("a", ok=False)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    reports = engine.apply()
    assert len(reports) == 1
    assert reports[0].ok is False
    assert b.calls == []  # never reached


def test_engine_apply_runs_all_when_all_succeed():
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    reports = engine.apply()
    assert len(reports) == 2
    assert all(r.ok for r in reports)


def test_engine_apply_notifies_on_step_start_and_done_per_step():
    """#574 round12 item 3: `on_step` fires around EVERY ladder step's own
    apply, in ladder order — the signal `boot_flow.run_migration()` needs
    to show phase-3 progress for the 8 domain steps, which otherwise
    produce zero progress events of their own."""
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    events: list[tuple[str, str]] = []
    engine = MigrationEngine([a, b], on_step=lambda step_id, kind: events.append((step_id, kind)))
    engine.apply()
    assert events == [("a", "start"), ("a", "done"), ("b", "start"), ("b", "done")]


def test_engine_apply_on_step_stops_notifying_after_a_failure():
    a = _FakeStep("a", ok=False)
    b = _FakeStep("b", ok=True)
    events: list[tuple[str, str]] = []
    engine = MigrationEngine([a, b], on_step=lambda step_id, kind: events.append((step_id, kind)))
    engine.apply()
    assert events == [("a", "start"), ("a", "done")]  # b never reached


def test_engine_apply_on_step_observer_failure_never_breaks_apply():
    a = _FakeStep("a", ok=True)

    def _raise(_step_id, _kind):
        raise RuntimeError("ui crashed")

    engine = MigrationEngine([a], on_step=_raise)
    reports = engine.apply()
    assert reports[0].ok is True


def test_engine_inspect_plan_dry_run_always_run_every_step():
    a = _FakeStep("a", ok=False)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    assert len(engine.inspect()) == 2
    assert len(engine.plan()) == 2
    assert len(engine.dry_run()) == 2


def test_engine_rollback_runs_in_reverse_order(tmp_path):
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b], data_home=tmp_path)
    engine.rollback()
    assert a.calls == ["rollback"]
    assert b.calls == ["rollback"]


def test_engine_rollback_stops_on_first_failure_in_reverse_order(tmp_path):
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=False)
    engine = MigrationEngine([a, b], data_home=tmp_path)
    reports = engine.rollback()
    assert len(reports) == 1
    assert reports[0].step_id == "b"
    assert a.calls == []  # never reached, b failed first in reverse order


def test_engine_rollback_without_data_home_raises_instead_of_silently_skipping(monkeypatch):
    """#350 qa follow-up: `MigrationEngine([...])` with no data_home used to
    make rollback() silently skip deleting the V2 root via a bare
    `if self._data_home is not None:` guard — nothing asserted that
    shutil.rmtree was never called, so a future edit could delete that guard
    with no test catching it (and storage_layout_v2(None) would then fall
    back to the real config.DATA_HOME, per the module docstring's warning).
    rollback() must now refuse outright, and this proves shutil.rmtree is
    never even reached to do so."""
    calls: list[object] = []
    monkeypatch.setattr(
        "agent_takkub.core.migration.engine.shutil.rmtree",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])  # no data_home

    with pytest.raises(RuntimeError, match="data_home"):
        engine.rollback()

    assert calls == []


def test_engine_apply_downgrades_step_ok_when_a_later_step_corrupts_its_target():
    """#350: each step's apply() only ever checks its own write — a LATER
    step overwriting an EARLIER step's already-applied target still lets
    both report ok:true individually. `apply()` must catch that itself by
    re-validating the whole ladder once every step has run, not rely on a
    separate `validate` call to be the first to notice."""

    class _CorruptingStep(_FakeStep):
        """apply() reports ok:true (its own write succeeded), but its
        target is already gone by the time anyone checks validate() —
        standing in for a later step in the ladder having clobbered it in
        between, exactly like `runtime-triage` did to `state`'s targets."""

        def validate(self) -> StepReport:
            self.calls.append("validate")
            return StepReport(self.step_id, "validate", False, "clobbered by later step")

    a = _CorruptingStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    reports = engine.apply()
    assert len(reports) == 2
    assert reports[0].ok is False
    assert "clobbered by later step" in reports[0].summary
    assert reports[1].ok is True


def test_engine_rollback_does_not_wipe_a_v2_root_promote_rollback_just_restored(tmp_path):
    """#504 B5 (2026-09-10 acceptance review, `engine_rollback` repro): a
    real promote+archive pair, then a whole-ladder `rollback()` — the final
    cleanup used to unconditionally `shutil.rmtree` `DATA_HOME/v2`, wiping
    out the real data `PromoteV2RootStep.rollback()` (run moments earlier,
    in the same call) had just recreated there as the ONLY remaining copy."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.promote_v1 import ArchiveV1LegacyStep, PromoteV2RootStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore

    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "only.json").write_text("unique", encoding="utf-8")

    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert promote.apply().ok
    assert archive.apply().ok

    engine = MigrationEngine([promote, archive], data_home=data_home, journal=journal)
    reports = engine.rollback()

    assert all(r.ok for r in reports), [(r.step_id, r.summary) for r in reports]
    assert list(data_home.rglob("only.json"))


def test_engine_apply_stops_pass2_prune_after_an_earlier_step_leaves_a_duplicate(tmp_path):
    """#504 round5 R5-H1 (`prune_failure_stops_later_prunes`): the pass-1
    barrier only gates whether pass 2 (deferred prune) STARTS — once
    started, `promote-v2-root`'s own `prune()` failing partway through
    (a denied removal, recorded DUPLICATE) must stop `archive-v1-legacy`
    from pruning its OWN V1 sources in the SAME pass. Before this fix the
    loop had no early exit and kept pruning every later step regardless."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.promote_v1 import ArchiveV1LegacyStep, PromoteV2RootStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore

    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("A", encoding="utf-8")
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("B", encoding="utf-8")
    (data_home / "legacy-top.json").write_text("LEGACY", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_remove = promote_mod._remove
    denied = data_home / "v2" / "state"

    def _deny_state_removal(path):
        if path == denied:
            raise PermissionError("prune denied")
        return real_remove(path)

    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    engine = MigrationEngine([promote, archive], data_home=data_home, journal=journal)

    import unittest.mock as mock

    with mock.patch.object(promote_mod, "_remove", side_effect=_deny_state_removal):
        reports = engine.apply()

    promote_report = next(r for r in reports if r.step_id == "promote-v2-root")
    archive_report = next(r for r in reports if r.step_id == "archive-v1-legacy")
    assert not promote_report.ok
    assert "DUPLICATE" in promote_report.summary
    # `archive-v1-legacy` copy-verified `legacy-top.json` this same pass —
    # its own prune() must never have run, so the V1 source is untouched.
    assert archive_report.ok
    assert (data_home / "legacy-top.json").read_text(encoding="utf-8") == "LEGACY"
    assert list(data_home.parent.rglob("legacy-top.json"))


def test_apply_version_marker_only_runs_just_step_zero(tmp_path):
    """#361 boot fast-path: `apply_version_marker_only()` must touch only
    the version marker, never re-walk the rest of the ladder."""
    a = _FakeStep("version-marker", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b], data_home=tmp_path)
    report = engine.apply_version_marker_only()
    assert report.ok is True
    assert a.calls == ["apply"]
    assert b.calls == []  # never touched


def test_applied_step_ids_empty_without_a_journal():
    engine = MigrationEngine([_FakeStep("a")])  # no journal, no data_home
    assert engine.applied_step_ids() == []


def test_applied_step_ids_delegates_to_the_engine_journal(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    journal.record("a", "apply", True)
    engine = MigrationEngine([_FakeStep("a")], data_home=tmp_path, journal=journal)
    assert engine.applied_step_ids() == ["a"]


class TestApplyPending:
    """#362: `apply_pending()` runs only what a mixed-state machine still
    needs — closing the gap where boot's old step-1-only fast path never
    walked a ladder step added after this machine's first full apply."""

    def test_never_applied_step_runs_and_is_validated_right_after(self, tmp_path):
        """#574 round14b: a freshly-applied step is now validated IMMEDIATELY
        after its own apply, in the SAME `apply_pending()` call — see that
        method's own note for why (phase-3 UI timing)."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=True)
        engine = MigrationEngine([a], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        assert a.calls == ["apply", "validate"]
        assert [r.step_id for r in reports] == ["a"]
        assert [r.step_id for r in engine.last_validate_reports] == ["a"]
        assert engine.last_validate_reports[0].ok is True

    def test_applied_and_still_valid_step_is_skipped_entirely(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        journal.record("a", "apply", True)
        a = _FakeStep("a", ok=True)  # validate() also reports ok=True
        engine = MigrationEngine([a], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        assert a.calls == ["validate"]  # probed, found healthy, never re-applied
        assert reports == []

    def test_applied_but_now_invalid_step_is_reapplied(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        journal.record("a", "apply", True)

        class _Stale(_FakeStep):
            """Stands in for version-marker after an app upgrade, or any
            step whose target got clobbered — journal says applied, but
            validate() disagrees right now."""

            def validate(self) -> StepReport:
                self.calls.append("validate")
                return StepReport(self.step_id, "validate", False, "stale")

        a = _Stale("a", ok=True)
        engine = MigrationEngine([a], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        # #574 round14b: the FIRST "validate" is the staleness probe above
        # (`applied_before` check); the SECOND is the immediate post-apply
        # validate `apply_pending()` now does for every step it just
        # applied.
        assert a.calls == ["validate", "apply", "validate"]
        assert [r.step_id for r in reports] == ["a"]
        assert reports[0].ok is True

    def test_on_step_notifies_only_for_steps_actually_applied(self, tmp_path):
        """#574 round12 item 3: a step skipped outright (applied + still
        valid — no `apply()` call at all, per this method's own docstring)
        must not fire `on_step` either; only steps this call genuinely
        applies get a start/done pair."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        journal.record("a", "apply", True)
        a = _FakeStep("a", ok=True)  # already applied + still valid -> skipped
        b = _FakeStep("b", ok=True)  # never applied -> runs
        events: list[tuple[str, str]] = []
        engine = MigrationEngine(
            [a, b],
            data_home=tmp_path,
            journal=journal,
            on_step=lambda step_id, kind: events.append((step_id, kind)),
        )
        engine.apply_pending()
        assert events == [("b", "start"), ("b", "done")]

    def test_skip_step_ids_holds_back_a_specific_step_regardless_of_pending_ness(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=False)  # never applied -> would normally run
        b = _FakeStep("b", ok=True)
        engine = MigrationEngine([a, b], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending(skip_step_ids={"a"})
        assert a.calls == []
        assert [r.step_id for r in reports] == ["b"]

    def test_does_not_stop_the_line_on_an_earlier_pending_steps_failure(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=False)
        b = _FakeStep("b", ok=True)
        engine = MigrationEngine([a, b], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        assert [r.step_id for r in reports] == ["a", "b"]
        assert reports[0].ok is False
        assert reports[1].ok is True

    def test_pre_migrate_backup_failure_stops_the_pass_before_anything_else_runs(self, tmp_path):
        """#504/#574 R8-H1: `apply_pending()`'s own "No stop-the-line"
        contract (see the test right above) does NOT apply to
        `pre-migrate-backup` — a failed backup must abort the whole ladder
        before anything else is touched, matching `apply()`'s own free
        stop-the-line for this step. Without this, the reviewed
        `backup_failure_aborts_apply_pending` repro had the pass walk 12
        more steps, overwriting `models/registry.json` and `runtime/core/
        version.json` with no usable pre-migrate backup in existence."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("pre-migrate-backup", ok=False)
        b = _FakeStep("version-marker", ok=True)
        c = _FakeStep("readonly-registries", ok=True)
        engine = MigrationEngine([a, b, c], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        assert [r.step_id for r in reports] == ["pre-migrate-backup"]
        assert reports[0].ok is False
        assert b.calls == []
        assert c.calls == []

    def test_promote_v2_root_failure_stops_the_pass_before_archive_runs(self, tmp_path):
        """#504 B2 (acceptance review): `archive-v1-legacy` treats a
        non-empty legacy `v2/` as safe to remove the instant it exists — a
        `promote-v2-root` failure in THIS SAME pass must stop the whole
        pass right there, never reaching `archive-v1-legacy` with a `v2/`
        that never finished draining."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("promote-v2-root", ok=False)
        b = _FakeStep("archive-v1-legacy", ok=True)
        engine = MigrationEngine([a, b], data_home=tmp_path, journal=journal)
        reports = engine.apply_pending()
        assert [r.step_id for r in reports] == ["promote-v2-root"]
        # `apply_pending()`'s own `v1_retired` pre-check always calls
        # `archive_step.validate()` up front regardless — the important
        # part is `apply()` itself is never reached.
        assert "apply" not in b.calls

    def test_prod_today_machine_applies_only_the_ladder_step_added_after_it_migrated(
        self, tmp_path
    ):
        """Reproduces the exact prod gap (#360 following #361): 8 ladder
        steps already applied under the pre-#360 ladder; `core-internal-
        store` (added by #360) never applied. `apply_pending()` must run
        only that one new step, never re-touch the 8 that already
        succeeded."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        for step_id in (
            "version-marker",
            "readonly-registries",
            "role-agent",
            "capability",
            "project",
            "state",
            "credential-reference",
            "runtime-triage",
        ):
            journal.record(step_id, "apply", True)
        old_steps = [_FakeStep(step_id, ok=True) for step_id in journal.applied_step_ids()]
        new_step = _FakeStep("core-internal-store", ok=True)
        engine = MigrationEngine([*old_steps, new_step], data_home=tmp_path, journal=journal)

        reports = engine.apply_pending()

        assert [r.step_id for r in reports] == ["core-internal-store"]
        assert all(s.calls == ["validate"] for s in old_steps)
        # #574 round14b: freshly-applied steps are validated immediately.
        assert new_step.calls == ["apply", "validate"]


class TestValidateOkSteps:
    """#504/#574 R8-M2: `apply_pending()` has no whole-ladder `validate()`
    pass the way `apply()` does — `validate_ok_steps()` is what
    `auto_migrate_boot._run_apply_pending` calls afterward for just the
    step ids it applied successfully, so `MigrationOutcome.validated_steps`
    stops being permanently 0 on every promoted machine."""

    def test_returns_real_validate_reports_for_only_the_named_steps(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=True)
        b = _FakeStep("b", ok=False)
        c = _FakeStep("c", ok=True)
        engine = MigrationEngine([a, b, c], data_home=tmp_path, journal=journal)

        reports = engine.validate_ok_steps(["a", "c"])

        assert [r.step_id for r in reports] == ["a", "c"]
        assert all(r.ok for r in reports)
        assert a.calls == ["validate"]
        assert b.calls == []  # never asked — not in the requested set
        assert c.calls == ["validate"]

    def test_never_truncated_by_an_unrelated_steps_own_failure(self, tmp_path):
        """Unlike the whole-ladder `validate()`, which stops at the first
        `ok=False` report, this must keep going — a caller asking about
        steps THIS pass applied successfully must not have that answer cut
        short by some other, unrelated step failing its own validate()."""
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=True)
        broken = _FakeStep("broken", ok=False)
        c = _FakeStep("c", ok=True)
        engine = MigrationEngine([a, broken, c], data_home=tmp_path, journal=journal)

        reports = engine.validate_ok_steps(["a", "c"])

        assert [r.step_id for r in reports] == ["a", "c"]
        assert all(r.ok for r in reports)
        assert broken.calls == []

    def test_ladder_order_regardless_of_requested_order(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=True)
        b = _FakeStep("b", ok=True)
        engine = MigrationEngine([a, b], data_home=tmp_path, journal=journal)

        reports = engine.validate_ok_steps(["b", "a"])

        assert [r.step_id for r in reports] == ["a", "b"]


class TestOnValidateStep:
    """#574 round14 (R5-M3/R8-M2 remainder): `boot_flow.py`'s phase-3 UI
    must read from each step's REAL `validate()` result, never from
    `on_step` (apply-time, no pass/fail of its own to report)."""

    def test_validate_notifies_per_step_with_the_real_ok(self):
        a = _FakeStep("a", ok=True)
        b = _FakeStep("b", ok=False)
        events: list[tuple[str, bool]] = []
        engine = MigrationEngine(
            [a, b], on_validate_step=lambda step_id, ok: events.append((step_id, ok))
        )
        engine.validate()
        assert events == [("a", True), ("b", False)]

    def test_validate_stops_notifying_after_a_failure(self):
        a = _FakeStep("a", ok=False)
        b = _FakeStep("b", ok=True)
        events: list[tuple[str, bool]] = []
        engine = MigrationEngine(
            [a, b], on_validate_step=lambda step_id, ok: events.append((step_id, ok))
        )
        engine.validate()
        assert events == [("a", False)]  # b never reached — validate() stop-the-line

    def test_validate_on_validate_step_observer_failure_never_breaks_validate(self):
        a = _FakeStep("a", ok=True)

        def _raise(_step_id, _ok):
            raise RuntimeError("ui crashed")

        engine = MigrationEngine([a], on_validate_step=_raise)
        reports = engine.validate()
        assert reports[0].ok is True

    def test_validate_ok_steps_notifies_only_the_requested_steps(self, tmp_path):
        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        a = _FakeStep("a", ok=True)
        b = _FakeStep("b", ok=True)
        c = _FakeStep("c", ok=False)
        events: list[tuple[str, bool]] = []
        engine = MigrationEngine(
            [a, b, c],
            data_home=tmp_path,
            journal=journal,
            on_validate_step=lambda step_id, ok: events.append((step_id, ok)),
        )
        engine.validate_ok_steps(["a", "c"])
        assert events == [("a", True), ("c", False)]  # ladder order, b never asked


class TestApplyPendingInlineValidateTiming:
    """#574 round14b: `apply_pending()` validates a freshly-applied step
    immediately after its own apply, in ladder order — never batched into
    a separate call after the whole pass (including a real
    `archive-v1-legacy`'s own copy phase) has already run. This is what
    keeps `boot_flow.py`'s phase-3 UI notification for a domain step from
    landing after phase 4 has already started (the round8/9 acceptance
    harnesses' `progress_schema`/`progress_unit_stable`/
    `progress_file_counter_monotonic` regression)."""

    def test_notifies_a_domain_step_before_a_prune_deferred_steps_own_start(self, tmp_path):
        class _PruneDeferredLike(_FakeStep):
            def prune(self) -> StepReport:
                self.calls.append("prune")
                return StepReport(self.step_id, "prune", True, "pruned")

            def _health_problems(self) -> list[str]:
                return []

        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        domain = _FakeStep("readonly-registries", ok=True)
        archive = _PruneDeferredLike("archive-v1-legacy", ok=True)
        events: list[tuple] = []
        engine = MigrationEngine(
            [domain, archive],
            data_home=tmp_path,
            journal=journal,
            on_step=lambda step_id, kind: events.append(("step", step_id, kind)),
            on_validate_step=lambda step_id, ok: events.append(("validate", step_id, ok)),
        )
        engine.apply_pending()
        assert events.index(("validate", "readonly-registries", True)) < events.index(
            ("step", "archive-v1-legacy", "start")
        )

    def test_suppresses_live_notify_but_still_counts_when_promote_has_nothing_pending(
        self, tmp_path
    ):
        """The R8-M2 residual-catch-up case: a domain step with genuinely
        new work still gets a real `validate()` (`last_validate_reports`
        must include it, so `MigrationOutcome.validated_steps` is never
        falsely 0), but phase 3 is part of the migration WIZARD's own
        screen sequence — with nothing left to promote this pass, there is
        no active migration screen for a residual catch-up to appear in."""

        class _PromoteLike(_FakeStep):
            def __init__(self, *a, candidates=(), **kw):
                super().__init__(*a, **kw)
                self._candidates = list(candidates)

            def _promote_candidates(self) -> list[str]:
                return list(self._candidates)

            def prune(self) -> StepReport:
                self.calls.append("prune")
                return StepReport(self.step_id, "prune", True, "pruned")

            def _health_problems(self) -> list[str]:
                return []

        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        promote = _PromoteLike("promote-v2-root", ok=True, candidates=[])
        domain = _FakeStep("readonly-registries", ok=True)
        events: list[tuple[str, bool]] = []
        engine = MigrationEngine(
            [promote, domain],
            data_home=tmp_path,
            journal=journal,
            on_validate_step=lambda step_id, ok: events.append((step_id, ok)),
        )
        engine.apply_pending()
        assert ("readonly-registries", True) not in events
        assert [r.step_id for r in engine.last_validate_reports] == [
            "readonly-registries",
            "promote-v2-root",
        ]
        assert all(r.ok for r in engine.last_validate_reports)

    def test_notifies_normally_when_promote_has_real_pending_work(self, tmp_path):
        class _PromoteLike(_FakeStep):
            def __init__(self, *a, candidates=(), **kw):
                super().__init__(*a, **kw)
                self._candidates = list(candidates)

            def _promote_candidates(self) -> list[str]:
                return list(self._candidates)

            def prune(self) -> StepReport:
                self.calls.append("prune")
                return StepReport(self.step_id, "prune", True, "pruned")

            def _health_problems(self) -> list[str]:
                return []

        journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
        promote = _PromoteLike("promote-v2-root", ok=True, candidates=["models"])
        domain = _FakeStep("readonly-registries", ok=True)
        events: list[tuple[str, bool]] = []
        engine = MigrationEngine(
            [promote, domain],
            data_home=tmp_path,
            journal=journal,
            on_validate_step=lambda step_id, ok: events.append((step_id, ok)),
        )
        engine.apply_pending()
        assert ("readonly-registries", True) in events


class TestRollbackStep:
    def test_rolls_back_only_the_named_step(self):
        a = _FakeStep("a", ok=True)
        b = _FakeStep("b", ok=True)
        engine = MigrationEngine([a, b])
        report = engine.rollback_step("b")
        assert report.step_id == "b"
        assert a.calls == []
        assert b.calls == ["rollback"]

    def test_unknown_step_id_raises_key_error(self):
        engine = MigrationEngine([_FakeStep("a", ok=True)])
        with pytest.raises(KeyError):
            engine.rollback_step("does-not-exist")


def test_full_ladder_apply_validate_rollback_no_cross_step_data_loss(tmp_path, monkeypatch):
    """#350 regression: run the whole V1->V2 ladder end to end on a fixture.
    Proves (a) apply()+validate() both stay ok:true for every step, (b) the
    `state` step's own targets (autoresume/remote-sessions) survive the
    later `runtime-triage` step untouched, (c) `dry_run()` right after apply
    shows 0 targets would change, and (d) a full rollback actually returns
    `doctor --storage-layout` to "v1" instead of getting stuck on "mixed"."""
    from agent_takkub.core.migration.steps_v1 import (
        CredentialReferenceStep,
        ProjectMigrationStep,
        RoleAgentMigrationStep,
        RuntimeTriageStep,
        build_capability_step,
        build_readonly_registries_step,
        build_state_step,
    )
    from agent_takkub.core.storage.layout import layout_state, storage_layout_v2
    from agent_takkub.core.storage.legacy_reader import read_json

    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    runtime_dir = tmp_path / "runtime"
    custom_agents_dir = tmp_path / "custom-agents"
    for d in (data_home, settings_home, runtime_dir, custom_agents_dir):
        d.mkdir(parents=True)
    monkeypatch.setattr(
        "agent_takkub.core.migration.steps.version_doc_path", lambda: tmp_path / "version.json"
    )

    (settings_home / "autoresume.json").write_text(json.dumps({"on": True}), encoding="utf-8")
    (settings_home / "takkub-remote-sessions.json").write_text(
        json.dumps({"remote": True}), encoding="utf-8"
    )
    (data_home / "projects.json").write_text(
        json.dumps({"active": None, "projects": {}}), encoding="utf-8"
    )
    (runtime_dir / "sessions" / "2026-08-22" / "demo").mkdir(parents=True)
    (runtime_dir / "sessions" / "2026-08-22" / "demo" / "backend.md").write_text(
        "note", encoding="utf-8"
    )

    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    steps = [
        VersionMarkerStep(journal=journal, backups=backups),
        build_readonly_registries_step(
            journal, backups, data_home=data_home, settings_home=settings_home
        ),
        RoleAgentMigrationStep(
            journal=journal,
            backups=backups,
            data_home=data_home,
            settings_home=settings_home,
            custom_agents_dir=custom_agents_dir,
        ),
        build_capability_step(journal, backups, data_home=data_home, settings_home=settings_home),
        ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home),
        build_state_step(journal, backups, data_home=data_home, settings_home=settings_home),
        CredentialReferenceStep(
            journal=journal, backups=backups, data_home=data_home, refs_override={}
        ),
        RuntimeTriageStep(
            journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
        ),
    ]
    engine = MigrationEngine(steps, data_home=data_home)

    apply_reports = engine.apply()
    assert all(r.ok for r in apply_reports), [(r.step_id, r.summary) for r in apply_reports]

    validate_reports = engine.validate()
    assert all(r.ok for r in validate_reports), [(r.step_id, r.summary) for r in validate_reports]

    dry_reports = engine.dry_run()
    state_dry = next(r for r in dry_reports if r.step_id == "state")
    assert state_dry.detail["would_change"] == []

    layout = storage_layout_v2(data_home)
    assert read_json(layout.state_sessions / "autoresume.json")["data"] == {"on": True}
    assert read_json(layout.state_sessions / "remote.json")["data"] == {"remote": True}
    assert (layout.state_sessions / "2026-08-22" / "demo" / "backend.md").exists()

    assert layout_state(data_home) == "mixed"

    rollback_reports = engine.rollback()
    assert all(r.ok for r in rollback_reports), [(r.step_id, r.summary) for r in rollback_reports]
    assert layout_state(data_home) == "v1"


# ---------------------------------------------------------------------------
# CoreInternalStoreStep (#360 — core_home() -> v2/system/)
# ---------------------------------------------------------------------------


def _core_internal_step_fixture(tmp_path):
    from agent_takkub.core.migration.steps_v1 import CoreInternalStoreStep

    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    source = runtime_dir / "core"
    source.mkdir(parents=True)
    (source / "version.json").write_text(json.dumps({"app": "1.1.0"}), encoding="utf-8")
    (source / "accounts.jsonl").write_text(
        json.dumps({"id": "acct-1", "provider_id": "claude"}) + "\n", encoding="utf-8"
    )
    (source / "conversations" / "proj" / "conv-1").mkdir(parents=True)
    (source / "conversations" / "proj" / "conv-1" / "messages.jsonl").write_text(
        "hello\n", encoding="utf-8"
    )

    journal = MigrationJournal(JsonlStore(source / "migration_journal.jsonl"))
    journal.record("some-earlier-step", "apply", True, "unrelated")
    backups = BackupManager(source / "migration_backups")

    step = CoreInternalStoreStep(
        journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
    )
    return step, data_home, source


def test_core_internal_store_step_happy_path_copies_into_v2_system(tmp_path):
    step, data_home, source = _core_internal_step_fixture(tmp_path)
    target = data_home / "system"

    inspect = step.inspect()
    assert set(inspect.detail["entries"]) == {"version.json", "accounts.jsonl", "conversations"}

    report = step.apply()
    assert report.ok, report.summary
    assert (target / "version.json").read_text(encoding="utf-8") == (
        source / "version.json"
    ).read_text(encoding="utf-8")
    assert (target / "accounts.jsonl").read_text(encoding="utf-8") == (
        source / "accounts.jsonl"
    ).read_text(encoding="utf-8")
    assert (target / "conversations" / "proj" / "conv-1" / "messages.jsonl").read_text(
        encoding="utf-8"
    ) == "hello\n"

    validate = step.validate()
    assert validate.ok, validate.summary


def test_core_internal_store_step_never_copies_journal_or_backups(tmp_path):
    step, data_home, source = _core_internal_step_fixture(tmp_path)
    target = data_home / "system"

    excluded = set(step.inspect().detail["excluded"])
    # #574: `pre-migrate-backup`'s own marker/WAL files live in this SAME
    # source directory (`RUNTIME_DIR/core`) for the identical reason.
    assert excluded == {
        "migration_journal.jsonl",
        "migration_backups",
        "pre-migrate-backup-dir.txt",
        "pre-migrate-backup-wal.json",
    }

    report = step.apply()
    assert report.ok, report.summary
    assert not (target / "migration_journal.jsonl").exists()
    assert not (target / "migration_backups").exists()
    # the journal/backups this very step wrote through are still intact at
    # their original location — never moved, never deleted
    assert (source / "migration_journal.jsonl").exists()
    assert step.journal.applied_step_ids() == ["some-earlier-step", "core-internal-store"]


def test_core_internal_store_step_fallback_flips_only_after_the_final_rename(tmp_path, monkeypatch):
    """core_home() must see either the fully-populated old location or the
    fully-populated new one — never a system/ dir that only has some
    entries in it. A copy failure partway through staging must leave the
    real `system/` target absent entirely, not half-written."""
    from agent_takkub.core.storage.paths import core_home

    step, data_home, source = _core_internal_step_fixture(tmp_path)
    target = data_home / "system"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", data_home / "runtime")

    assert core_home() != target
    assert core_home() == source

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated failure mid-copy")
        return real_copy2(src, dst, *a, **k)

    shutil.copy2 = flaky_copy2
    try:
        report = step.apply()
    finally:
        shutil.copy2 = real_copy2
    assert report.ok is False

    # the failed run must never have made the real target visible at all
    assert not target.exists()
    assert core_home() == source

    # a clean retry (no more injected failures) succeeds fully
    report = step.apply()
    assert report.ok, report.summary
    assert core_home() == target


def test_core_internal_store_step_rollback_removes_freshly_created_target(tmp_path):
    step, data_home, source = _core_internal_step_fixture(tmp_path)
    target = data_home / "system"

    apply_report = step.apply()
    assert apply_report.ok, apply_report.summary
    assert target.is_dir()

    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert not target.exists()
    # the pre-migration source is untouched throughout (copy-never-move)
    assert (source / "version.json").exists()


def test_core_internal_store_step_rollback_restores_prior_reapply_state(tmp_path):
    """A re-apply onto an already-materialized target must be reversible
    too: rollback restores exactly what was there before THIS apply, not
    an empty target."""
    step, data_home, source = _core_internal_step_fixture(tmp_path)
    target = data_home / "system"

    first = step.apply()
    assert first.ok, first.summary

    (source / "accounts.jsonl").write_text(
        json.dumps({"id": "acct-1", "provider_id": "claude"})
        + "\n"
        + json.dumps({"id": "acct-2", "provider_id": "codex"})
        + "\n",
        encoding="utf-8",
    )
    second = step.apply()
    assert second.ok, second.summary
    assert "acct-2" in (target / "accounts.jsonl").read_text(encoding="utf-8")

    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert target.is_dir()  # restored, not removed — it existed before this apply
    assert "acct-2" not in (target / "accounts.jsonl").read_text(encoding="utf-8")


def test_core_internal_store_step_parity_with_real_registries(tmp_path, monkeypatch):
    """#360 parity requirement: real accounts/model_catalog registries must
    read back identical data once core_home() flips from RUNTIME_DIR/core
    to v2/system/ — proves the existing legacy-fallback in
    core.storage.paths actually works end to end, not just that raw files
    land in the right place."""
    from agent_takkub.core.accounts.registry import AccountRegistry
    from agent_takkub.core.migration.steps_v1 import CoreInternalStoreStep
    from agent_takkub.core.model_catalog.registry import ModelRegistry
    from agent_takkub.core.models.account import ProviderAccount
    from agent_takkub.core.models.model import ModelDefinition
    from agent_takkub.core.storage.paths import core_home

    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    data_home.mkdir(parents=True)
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", runtime_dir)

    assert core_home() == runtime_dir / "core"

    AccountRegistry().upsert(ProviderAccount(id="acct-1", provider_id="claude"))
    ModelRegistry().upsert(ModelDefinition(id="model-1", provider_id="claude", name="Sonnet"))

    journal = MigrationJournal(JsonlStore(runtime_dir / "core" / "migration_journal.jsonl"))
    backups = BackupManager(runtime_dir / "core" / "migration_backups")
    step = CoreInternalStoreStep(
        journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
    )

    report = step.apply()
    assert report.ok, report.summary
    assert core_home() == data_home / "system"

    accounts_after = AccountRegistry().all()
    assert [a.id for a in accounts_after] == ["acct-1"]
    models_after = ModelRegistry().all()
    assert [m.id for m in models_after] == ["model-1"]


def test_core_internal_store_step_validate_and_rollback_survive_a_fresh_engine_after_apply(
    tmp_path, monkeypatch
):
    """Regression (#362): a SECOND process/engine constructed after apply()
    already flipped `core_home()` to `system/` must still see its
    journal/backups resolve under the fixed `migration_home()` (== source),
    never under `core_home()`'s dynamic target — otherwise rollback's
    `shutil.rmtree(target)` immediately gets undone by the very next
    `journal.record()` call, which `mkdir(parents=True)`s its own file's
    parent back into existence. Before the fix (journal/backups built on
    `core_home()`), this reproduced exactly: rollback reported ok, but
    `target` came back as an empty directory that `core_home()` then kept
    pointing at, hiding the real data still sitting under `source`."""
    from agent_takkub.core.migration.steps_v1 import CoreInternalStoreStep
    from agent_takkub.core.storage.paths import core_home, migration_home

    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    source = runtime_dir / "core"
    source.mkdir(parents=True)
    (source / "version.json").write_text(json.dumps({"app": "1.1.0"}), encoding="utf-8")
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", runtime_dir)

    # Engine A: journal/backups resolve under the fixed `migration_home()`,
    # which equals `source` before anything has migrated.
    assert core_home() == source
    assert migration_home() == source
    engine_a = CoreInternalStoreStep(data_home=data_home, runtime_dir=runtime_dir)
    apply_report = engine_a.apply()
    assert apply_report.ok, apply_report.summary

    # core_home() has now flipped to `system/` for anything constructed
    # from this point on — but migration_home() has NOT, by design.
    target = data_home / "system"
    assert core_home() == target
    assert migration_home() == source

    # Engine B: a fresh instance simulating a later process (e.g. a
    # subsequent boot's pre-flight validate, or a rollback invoked
    # separately) — its journal/backups still resolve under `source`,
    # unaffected by core_home()'s flip.
    engine_b = CoreInternalStoreStep(data_home=data_home, runtime_dir=runtime_dir)
    assert engine_b.journal.store_path.parent == source
    assert engine_b.backups.root.parent == source

    validate_report = engine_b.validate()
    assert validate_report.ok, validate_report.summary

    rollback_report = engine_b.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert not target.exists()


def test_core_internal_store_step_reapply_never_clobbers_version_marker(tmp_path, monkeypatch):
    """#486: on a store where `target` (v2/system) already exists from an
    earlier full-ladder run, re-running the WHOLE ladder used to have this
    (last) step's re-apply branch copy `source`'s frozen `version.json`
    straight back over `target`, undoing `version-marker` (step 0)'s
    fresh write earlier in the SAME apply() call — which `engine.py`'s
    `_verify_post_apply` (#350) then caught by re-validating every step and
    downgrading `version-marker`'s own apply report to failed, even though
    `version-marker` itself never regressed."""
    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr("agent_takkub.core.migration.steps.APP_VERSION", "1.0.0")

    # A store with a V1 marker already migrated once, at app version 1.0.0 —
    # `target` already exists and carries the old version.
    engine = MigrationEngine()
    first_reports = engine.apply()
    assert all(r.ok for r in first_reports), [(r.step_id, r.summary) for r in first_reports]
    target = data_home / "system"
    source = runtime_dir / "core"
    assert target.is_dir()
    assert json.loads((target / "version.json").read_text())["components"][0]["version"] == "1.0.0"

    # App upgraded — re-running the FULL ladder again (not apply_pending())
    # is exactly the #486 repro shape.
    monkeypatch.setattr("agent_takkub.core.migration.steps.APP_VERSION", "2.0.0")
    for _ in range(2):  # สองรอบยิ่งดี — idempotent on a second re-run too
        second_reports = engine.apply()
        assert all(r.ok for r in second_reports), [(r.step_id, r.summary) for r in second_reports]

        version_marker_report = next(r for r in second_reports if r.step_id == "version-marker")
        core_internal_report = next(r for r in second_reports if r.step_id == "core-internal-store")
        assert version_marker_report.ok, version_marker_report.summary
        assert core_internal_report.ok, core_internal_report.summary

        target_version = json.loads((target / "version.json").read_text())["components"][0][
            "version"
        ]
        assert target_version == "2.0.0"
        # source's copy is frozen from the first materialization — never
        # touched again, so it stays at the pre-upgrade version forever.
        source_version = json.loads((source / "version.json").read_text())["components"][0][
            "version"
        ]
        assert source_version == "1.0.0"

        validate_reports = engine.validate()
        assert all(r.ok for r in validate_reports), [
            (r.step_id, r.summary) for r in validate_reports
        ]


def test_core_internal_store_step_reapply_never_clobbers_cursor_file(tmp_path, monkeypatch):
    """#488 (follow-up #486): same silent-revert shape, but for
    `conversation_ingest_cursors.json` instead of `version.json`. V1's
    cursor store (`cursor_store.py`) resolves its path through
    `core_home()` on every call — no cached path — so once this step's
    first apply flips `core_home()` to `target`, live conversation
    ingestion keeps writing fresh cursors straight into `target` while
    `source`'s copy stays frozen at whatever it was during that first
    materialization. Before this fix, re-running the FULL ladder had this
    step's re-apply branch copy that frozen `source` file straight back
    over the live `target` one, silently reverting already-ingested
    progress — and `validate()` byte-compared them, so it could never
    agree the store was healthy either."""
    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    source = runtime_dir / "core"
    source.mkdir(parents=True)
    (source / "conversation_ingest_cursors.json").write_text(
        json.dumps({"claude::conv-1": "cursor-a"}), encoding="utf-8"
    )
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", runtime_dir)

    engine = MigrationEngine()
    first_reports = engine.apply()
    assert all(r.ok for r in first_reports), [(r.step_id, r.summary) for r in first_reports]
    target = data_home / "system"
    assert target.is_dir()
    assert json.loads((target / "conversation_ingest_cursors.json").read_text()) == {
        "claude::conv-1": "cursor-a"
    }

    # Live ingestion after the flip — exactly what a real set_cursor() write
    # looks like once core_home() resolves straight to target.
    (target / "conversation_ingest_cursors.json").write_text(
        json.dumps({"claude::conv-1": "cursor-b"}), encoding="utf-8"
    )

    for _ in range(2):  # idempotent on a second re-run too
        second_reports = engine.apply()
        assert all(r.ok for r in second_reports), [(r.step_id, r.summary) for r in second_reports]

        cursor_now = json.loads((target / "conversation_ingest_cursors.json").read_text())
        assert cursor_now == {"claude::conv-1": "cursor-b"}
        # source's copy is frozen from the first materialization — never
        # touched again.
        source_cursor = json.loads((source / "conversation_ingest_cursors.json").read_text())
        assert source_cursor == {"claude::conv-1": "cursor-a"}

        validate_reports = engine.validate()
        assert all(r.ok for r in validate_reports), [
            (r.step_id, r.summary) for r in validate_reports
        ]


def test_core_internal_store_step_apply_pending_stays_quiet_once_only_cursors_drifted(
    tmp_path, monkeypatch
):
    """#488: before this fix, `validate()` byte-compared the cursor file
    against a frozen `source` copy, so a live post-flip write into `target`
    made this step's `validate()` report False forever — and
    `apply_pending()`'s pending check (`applied_before and
    validate().ok`) then re-ran `core-internal-store` (re-copying the WHOLE
    store) on literally every boot. Confirms boot now stays quiet, and
    never re-copies over the live write, once nothing has actually
    drifted."""
    data_home = tmp_path / "data_home"
    runtime_dir = data_home / "runtime"
    source = runtime_dir / "core"
    source.mkdir(parents=True)
    (source / "conversation_ingest_cursors.json").write_text(
        json.dumps({"claude::conv-1": "cursor-a"}), encoding="utf-8"
    )
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", runtime_dir)

    engine = MigrationEngine()
    first_reports = engine.apply_pending()
    assert all(r.ok for r in first_reports), [(r.step_id, r.summary) for r in first_reports]
    assert "core-internal-store" in [r.step_id for r in first_reports]

    target = data_home / "system"
    assert target.is_dir()

    # Live ingestion after the flip — resolves straight through core_home(),
    # which now equals target.
    (target / "conversation_ingest_cursors.json").write_text(
        json.dumps({"claude::conv-1": "cursor-b"}), encoding="utf-8"
    )

    second_reports = engine.apply_pending()
    assert second_reports == []  # every step, including core-internal-store, validates green
    # never re-copied -> the live write survives untouched
    assert json.loads((target / "conversation_ingest_cursors.json").read_text()) == {
        "claude::conv-1": "cursor-b"
    }


# ---------------------------------------------------------------------------
# 2026-09-10 acceptance review, ROUND 2, #568 — R2-H9 `domain_integrity`:
# once `archive-v1-legacy` retires a domain step's V1 source,
# `MigrationEngine.validate()` used to substitute an unconditional
# ok:True ("nothing left to cross-check against") for that step forever —
# corrupting or deleting the V2 target itself still validated green.
# ---------------------------------------------------------------------------


def test_validate_catches_a_corrupted_domain_target_after_v1_retirement(tmp_path, monkeypatch):
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", data_home)

    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {}}}), encoding="utf-8"
    )
    engine = MigrationEngine()
    apply_reports = engine.apply()
    assert all(r.ok for r in apply_reports), [(r.step_id, r.summary) for r in apply_reports]
    assert all(r.ok for r in engine.validate())

    from agent_takkub.core.storage.layout import storage_layout_v2

    registry = storage_layout_v2(data_home).projects_root / "registry.json"
    registry.write_text("not-json", encoding="utf-8")

    reports = MigrationEngine(data_home=data_home).validate()
    assert not all(r.ok for r in reports)
    failed = next(r for r in reports if not r.ok)
    assert failed.step_id == "project"
    assert "unhealthy" in failed.summary


def test_validate_catches_a_present_but_null_required_domain_value(tmp_path, monkeypatch):
    """#504 R3 `domain_null_data`: a required key present but holding
    ``null`` (``{"data": null}``) used to pass `_domain_target_problems` —
    ``k not in data`` is only true when the key is ABSENT, not when it holds
    a legitimately-impossible value."""
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", data_home)

    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {}}}), encoding="utf-8"
    )
    engine = MigrationEngine()
    assert all(r.ok for r in engine.apply())
    assert all(r.ok for r in engine.validate())

    from agent_takkub.core.storage.layout import storage_layout_v2

    registry = storage_layout_v2(data_home).projects_root / "registry.json"
    registry.write_text(json.dumps({"data": None}), encoding="utf-8")

    reports = MigrationEngine(data_home=data_home).validate()
    assert not all(r.ok for r in reports)
    failed = next(r for r in reports if not r.ok)
    assert failed.step_id == "project"


# ---------------------------------------------------------------------------
# #605 — a domain step's own V1 source can be independently retired (already
# archived) even while the ladder-wide `v1_retired` flag is false for an
# UNRELATED reason (here: a generic stray top-level leftover standing in for
# either OS junk clutter or a different domain step's own not-yet-archived
# V1 leftover) — `apply_pending()` must still recognize that and never
# re-derive/overwrite that step's already-migrated V2 target from its
# now-missing source.
# ---------------------------------------------------------------------------


def test_apply_pending_keeps_project_registry_when_v1_retired_flag_is_false_but_step_source_gone(
    tmp_path, monkeypatch
):
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", data_home)

    (data_home / "projects.json").write_text(
        json.dumps({"projects": {"demo": {"paths": {"web": "/tmp/web"}}}}), encoding="utf-8"
    )
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    engine = MigrationEngine(data_home=data_home, journal=journal)
    reports = engine.apply()
    assert all(r.ok for r in reports), [(r.step_id, r.summary) for r in reports]

    registry_path = storage_layout_v2(data_home).projects_root / "registry.json"
    registry_before = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_before["data"]["projects"]["demo"]["paths"] == {"web": "/tmp/web"}
    assert not (data_home / "projects.json").exists()  # archived away by the first apply()

    # A stray top-level leftover unrelated to the "project" step — another
    # domain step's own not-yet-archived V1 source (archive candidates are
    # an allow-list of real V1 names, 2026-09-23 review) — forces
    # `archive-v1-legacy`'s own validate() (and thus `v1_retired`) false on
    # the next pass, WITHOUT touching `projects.json`'s own
    # already-archived state.
    (data_home / "skill-policy.json").write_text("{}", encoding="utf-8")
    archive_step = engine.get_step("archive-v1-legacy")
    assert archive_step.validate().ok is False  # confirms v1_retired would be False

    engine2 = MigrationEngine(data_home=data_home, journal=journal)
    engine2.apply_pending()

    registry_after = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_after == registry_before  # "project" step never re-applied over its gone source


# ---------------------------------------------------------------------------
# #576 — apply_pending validates all domain steps regardless of pending
# ---------------------------------------------------------------------------


def test_apply_pending_counts_skipped_valid_domain_steps_in_validate_reports(tmp_path, monkeypatch):
    """#576: when apply_pending() is called and some domain steps are already
    applied and still valid (so they're skipped), they should still be
    included in last_validate_reports so that MigrationOutcome.validated_steps
    counts all valid steps, not just the ones that were re-applied. This is
    especially important on the re-apply-after-restore path where most domain
    steps are already complete."""
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))

    # Create a simple ladder engine
    engine = MigrationEngine(
        data_home=data_home,
        journal=journal,
    )

    # First pass: apply everything
    reports = engine.apply()
    assert all(r.ok for r in reports)

    # Second pass: apply_pending should skip already-valid steps but still
    # count them in validate_reports. This is the re-apply-after-restore scenario
    # where most domain steps are already complete.
    engine2 = MigrationEngine(
        data_home=data_home,
        journal=journal,
    )
    engine2.apply_pending()

    # Even though most steps were skipped (already applied and valid),
    # last_validate_reports should include them for the count
    assert len(engine2.last_validate_reports) > 0
    # The validate_reports should include all the skipped steps that are still valid
    validate_count = len(
        [
            r
            for r in engine2.last_validate_reports
            if r.ok and r.step_id not in ("pre-migrate-backup", "archive-v1-legacy")
        ]
    )
    # Should have validated at least some domain steps
    assert validate_count > 0


# ---------------------------------------------------------------------------
# #634: quarantine stray V1 sources
# ---------------------------------------------------------------------------


def test_quarantine_stray_v1_sources_from_registry_copy_step(tmp_path):
    """#634: V1 source files that re-appeared after migration are moved to
    quarantine, not left in place where they could corrupt re-apply."""

    from agent_takkub.core.migration.engine import _quarantine_stray_sources
    from agent_takkub.core.migration.registry_copy_step import RegistryCopyStep, RegistryMapping

    data_home = tmp_path / "data"
    data_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Create V1 source and V2 target
    v1_source = data_home / "v1_registry.json"
    v1_source.write_text('{"old": "data"}', encoding="utf-8")

    v2_target = data_home / "v2" / "registry.json"
    v2_target.parent.mkdir(parents=True)
    v2_target.write_text(
        '{"schema": 1, "migrated_at": 1234.5, "data": {"new": "data"}}', encoding="utf-8"
    )

    # Create step with one mapping (source -> target)
    # Use "capability" which is in _ARCHIVED_SOURCE_STEP_IDS
    mapping = RegistryMapping("test", v1_source, v2_target)
    step = RegistryCopyStep(
        step_id="capability",
        mappings=(mapping,),
        backups=BackupManager(backups_dir),
    )

    # Verify stray_source_paths detects it (source exists + target has data)
    stray = step.stray_source_paths()
    assert stray == [v1_source]

    # Quarantine it
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    ok = _quarantine_stray_sources(step, step.step_id, journal, step.backups)
    assert ok is True

    # Verify source was moved (not copied)
    assert not v1_source.exists(), "source should be moved, not left in place"

    # Verify it's in quarantine
    quarantine_dir = backups_dir / "stray-v1-sources"
    assert quarantine_dir.is_dir()
    # Find the quarantine subdirectory (timestamp-based)
    timestamp_dirs = list(quarantine_dir.iterdir())
    assert len(timestamp_dirs) == 1
    quarantine_file = timestamp_dirs[0] / v1_source.name
    assert quarantine_file.exists()
    assert quarantine_file.read_text(encoding="utf-8") == '{"old": "data"}'

    # Verify journal has the quarantine event
    entries = journal.all_entries()
    quarantine_entries = [e for e in entries if e.action == "quarantine"]
    assert len(quarantine_entries) == 1
    assert quarantine_entries[0].ok is True
    assert "stray" in quarantine_entries[0].detail.lower()


def test_quarantine_stray_v1_sources_skip_if_no_strays(tmp_path):
    """#634: If there are no stray sources, quarantine succeeds silently."""
    from agent_takkub.core.migration.engine import _quarantine_stray_sources
    from agent_takkub.core.migration.registry_copy_step import RegistryCopyStep, RegistryMapping

    data_home = tmp_path / "data"
    data_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    v1_source = data_home / "v1_registry.json"  # doesn't exist
    v2_target = data_home / "v2" / "registry.json"
    v2_target.parent.mkdir(parents=True)
    v2_target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")

    mapping = RegistryMapping("test", v1_source, v2_target)
    step = RegistryCopyStep(
        step_id="capability",
        mappings=(mapping,),
        backups=BackupManager(backups_dir),
    )

    # No stray sources
    stray = step.stray_source_paths()
    assert stray == []

    # Quarantine returns True (nothing to do)
    ok = _quarantine_stray_sources(step, step.step_id, None, step.backups)
    assert ok is True


# ---------------------------------------------------------------------------
# #636: end-to-end tests for stray V1 source quarantine during apply_pending
# ---------------------------------------------------------------------------


def test_end_to_end_quarantine_stray_projects_json_after_migration(tmp_path):
    """#636 Test 1: Migrate complete DATA_HOME → apply_pending() with stray
    projects.json → registry unchanged byte-for-byte, projects.json moved to
    backup, root has no projects.json."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.steps_v1 import ProjectMigrationStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data"
    data_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Set up V2 registry with real project data (as if migration completed)
    layout = storage_layout_v2(data_home)
    layout.projects_root.mkdir(parents=True, exist_ok=True)
    v2_registry = layout.projects_root / "registry.json"
    registry_data = {
        "schema": 1,
        "migrated_at": 1234.5,
        "data": {"proj-1": "id-1", "proj-2": "id-2"},
    }
    v2_registry.write_text(json.dumps(registry_data), encoding="utf-8")

    # Now introduce a stray projects.json (empty, as if it re-appeared)
    stray_projects = data_home / "projects.json"
    stray_projects_content = '{"active":null,"projects":{},"open_tabs":[]}'
    stray_projects.write_text(stray_projects_content, encoding="utf-8")

    # Set up migration engine with journal, marking project step as already applied
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(backups_dir)
    project_step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)

    # Simulate that project step was already applied (present in journal)
    journal.record("project", "apply", True, "initial migration")

    # Create engine and apply pending
    engine = MigrationEngine([project_step], data_home=data_home, journal=journal)
    engine.apply_pending()

    # Verify registry is unchanged (byte-for-byte)
    assert v2_registry.exists()
    result_registry = json.loads(v2_registry.read_text(encoding="utf-8"))
    assert result_registry == registry_data, "registry should be unchanged after quarantine"

    # Verify projects.json was moved to quarantine, not left in data_home
    assert not stray_projects.exists(), "stray projects.json should be moved to quarantine"

    # Verify it's in quarantine
    quarantine_dir = backups_dir / "stray-v1-sources"
    assert quarantine_dir.is_dir(), "stray-v1-sources directory should exist"
    timestamp_dirs = list(quarantine_dir.iterdir())
    assert len(timestamp_dirs) == 1
    quarantine_file = timestamp_dirs[0] / "projects.json"
    assert quarantine_file.exists(), "projects.json should be in quarantine"
    assert quarantine_file.read_text(encoding="utf-8") == stray_projects_content, (
        "quarantined file content should match original"
    )

    # Verify journal recorded the quarantine action
    entries = journal.all_entries()
    quarantine_entries = [e for e in entries if e.action == "quarantine"]
    assert len(quarantine_entries) == 1
    assert quarantine_entries[0].step_id == "project"
    assert quarantine_entries[0].ok is True


def test_end_to_end_quarantine_stray_role_agent_files_individually(tmp_path):
    """#636 Test 2a: Stray custom-roles.json and role-providers.json are
    quarantined individually when they re-appear after migration."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data"
    data_home.mkdir()
    settings_home = tmp_path / "settings"
    settings_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Set up V2 routing with real role-agent data
    layout = storage_layout_v2(data_home)
    layout_agents = layout.agents / "custom"
    layout_agents.mkdir(parents=True, exist_ok=True)
    v2_routing = layout.config_dir / "routing.json"
    v2_routing.parent.mkdir(parents=True, exist_ok=True)
    routing_data = {
        "schema": 1,
        "migrated_at": 1234.5,
        "global": {"provider": "custom-role-1"},
        "projects": {},
    }
    v2_routing.write_text(json.dumps(routing_data), encoding="utf-8")

    v2_registry = layout_agents / "registry.json"
    registry_data = {"schema": 1, "migrated_at": 1234.5, "data": {"custom": "roles"}}
    v2_registry.write_text(json.dumps(registry_data), encoding="utf-8")

    # Introduce stray custom-roles.json
    stray_custom_roles = settings_home / "custom-roles.json"
    stray_custom_roles.write_text("{}", encoding="utf-8")

    # Set up migration engine
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(backups_dir)
    role_step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )

    # Simulate that role-agent step was already applied
    journal.record("role-agent", "apply", True, "initial migration")

    engine = MigrationEngine([role_step], data_home=data_home, journal=journal)
    engine.apply_pending()

    # Verify stray custom-roles.json was moved to quarantine
    assert not stray_custom_roles.exists(), "stray custom-roles.json should be moved"

    quarantine_dir = backups_dir / "stray-v1-sources"
    assert quarantine_dir.is_dir()
    timestamp_dirs = list(quarantine_dir.iterdir())
    assert len(timestamp_dirs) == 1
    quarantine_file = timestamp_dirs[0] / "custom-roles.json"
    assert quarantine_file.exists()

    # Verify routing and registry are unchanged
    assert json.loads(v2_routing.read_text(encoding="utf-8")) == routing_data
    assert json.loads(v2_registry.read_text(encoding="utf-8")) == registry_data


def test_end_to_end_quarantine_stray_role_agent_files_together(tmp_path):
    """#636 Test 2b: Multiple stray role-agent files (custom-roles.json and
    per-project role-providers.json) are quarantined together in one pass."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data"
    data_home.mkdir()
    settings_home = tmp_path / "settings"
    settings_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Set up V2 routing with real role-agent data
    layout = storage_layout_v2(data_home)
    layout_agents = layout.agents / "custom"
    layout_agents.mkdir(parents=True, exist_ok=True)
    v2_routing = layout.config_dir / "routing.json"
    v2_routing.parent.mkdir(parents=True, exist_ok=True)
    routing_data = {
        "schema": 1,
        "migrated_at": 1234.5,
        "global": {"provider": "custom-role-1"},
        "projects": {"my-project": {"provider": "per-project-role"}},
    }
    v2_routing.write_text(json.dumps(routing_data), encoding="utf-8")

    v2_registry = layout_agents / "registry.json"
    registry_data = {"schema": 1, "migrated_at": 1234.5, "data": {"custom": "roles"}}
    v2_registry.write_text(json.dumps(registry_data), encoding="utf-8")

    # Set up V1 projects.json so _project_names() can detect projects
    # (for finding per-project role-providers.json files to quarantine)
    v1_projects = data_home / "projects.json"
    v1_projects.write_text(
        json.dumps({"projects": {"my-project": "proj-id"}}),
        encoding="utf-8",
    )

    # Also set up V2 projects.json for stray detection
    layout_projects = layout.projects_root / "registry.json"
    layout_projects.parent.mkdir(parents=True, exist_ok=True)
    layout_projects.write_text(
        json.dumps({"schema": 1, "data": {"my-project": "proj-id"}}),
        encoding="utf-8",
    )

    # Introduce stray custom-roles.json and per-project role-providers.json
    stray_custom_roles = settings_home / "custom-roles.json"
    stray_custom_roles.write_text("{}", encoding="utf-8")

    # Per-project role-providers.json is sourced from settings_home/projects/<slug>/
    # slug is the project name with special chars (except . and -) converted to _
    # "my-project" stays as "my-project" since - is allowed
    stray_project_routing = settings_home / "projects" / "my-project" / "role-providers.json"
    stray_project_routing.parent.mkdir(parents=True, exist_ok=True)
    stray_project_routing.write_text("{}", encoding="utf-8")

    # Set up migration engine
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(backups_dir)
    role_step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )

    # Simulate that role-agent step was already applied
    journal.record("role-agent", "apply", True, "initial migration")

    engine = MigrationEngine([role_step], data_home=data_home, journal=journal)
    engine.apply_pending()

    # Verify both stray files were moved to quarantine
    assert not stray_custom_roles.exists()
    assert not stray_project_routing.exists()

    quarantine_dir = backups_dir / "stray-v1-sources"
    assert quarantine_dir.is_dir()
    timestamp_dirs = list(quarantine_dir.iterdir())
    assert len(timestamp_dirs) == 1
    quarantine_ts_dir = timestamp_dirs[0]

    # Both files should be in the same timestamp directory
    assert (quarantine_ts_dir / "custom-roles.json").exists()
    assert (quarantine_ts_dir / "role-providers.json").exists()

    # Verify routing and registry are unchanged
    assert json.loads(v2_routing.read_text(encoding="utf-8")) == routing_data
    assert json.loads(v2_registry.read_text(encoding="utf-8")) == registry_data

    # Verify journal recorded quarantine
    entries = journal.all_entries()
    quarantine_entries = [e for e in entries if e.action == "quarantine"]
    assert len(quarantine_entries) == 1
    assert "quarantined 2 stray V1 source file(s)" in quarantine_entries[0].detail


def test_end_to_end_quarantine_fails_when_move_fails(tmp_path):
    """#636 Test 3: If shutil.move() fails during quarantine, the step is
    skipped (not re-applied), target unchanged, and journal records ok=False."""
    import unittest.mock as mock

    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.steps_v1 import ProjectMigrationStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data"
    data_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Set up V2 registry with real project data
    layout = storage_layout_v2(data_home)
    layout.projects_root.mkdir(parents=True, exist_ok=True)
    v2_registry = layout.projects_root / "registry.json"
    registry_data = {
        "schema": 1,
        "migrated_at": 1234.5,
        "data": {"proj-1": "id-1"},
    }
    v2_registry.write_text(json.dumps(registry_data), encoding="utf-8")

    # Introduce stray projects.json
    stray_projects = data_home / "projects.json"
    stray_projects.write_text('{"active":null,"projects":{},"open_tabs":[]}', encoding="utf-8")

    # Set up migration engine
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(backups_dir)
    project_step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)

    # Simulate that project step was already applied
    journal.record("project", "apply", True, "initial migration")

    engine = MigrationEngine([project_step], data_home=data_home, journal=journal)

    # Monkeypatch shutil.move to fail
    with mock.patch("shutil.move", side_effect=OSError("move denied")):
        engine.apply_pending()

    # Verify stray file was NOT moved (move failed)
    assert stray_projects.exists(), "stray file should still exist when move fails"

    # Verify registry is unchanged
    assert json.loads(v2_registry.read_text(encoding="utf-8")) == registry_data

    # Verify step was skipped (not re-applied)
    # We verify this by checking that quarantine failed
    entries = journal.all_entries()
    quarantine_entries = [e for e in entries if e.action == "quarantine"]
    assert len(quarantine_entries) == 1
    assert quarantine_entries[0].ok is False
    assert "failed to quarantine" in quarantine_entries[0].detail.lower()


def test_end_to_end_stray_not_quarantined_on_fresh_install(tmp_path):
    """#636 Test 4: Fresh install (step never applied before) + have real V1
    files → must NOT be quarantined, normal migration should occur."""
    from agent_takkub.core.migration.backup import BackupManager
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.journal import MigrationJournal
    from agent_takkub.core.migration.steps_v1 import ProjectMigrationStep
    from agent_takkub.core.storage.jsonl_store import JsonlStore
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data"
    data_home.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Set up V1 source with real project data (fresh install, not migrated yet)
    v1_projects = data_home / "projects.json"
    v1_data = {
        "active": "proj-1",
        "projects": {"proj-1": "id-1", "proj-2": "id-2"},
        "open_tabs": ["tab-1"],
    }
    v1_projects.write_text(json.dumps(v1_data), encoding="utf-8")

    # NO V2 registry yet (fresh install)
    layout = storage_layout_v2(data_home)
    v2_registry = layout.projects_root / "registry.json"
    assert not v2_registry.exists()

    # Set up migration engine — project step NOT in journal (fresh install)
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(backups_dir)
    project_step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)

    engine = MigrationEngine([project_step], data_home=data_home, journal=journal)

    # Apply (fresh migration)
    reports = engine.apply()

    # Verify project migration succeeded and wrote V2 registry
    project_report = next(r for r in reports if r.step_id == "project")
    assert project_report.ok is True

    # Verify V2 registry was created with the migrated data
    assert v2_registry.exists()
    result_data = json.loads(v2_registry.read_text(encoding="utf-8"))
    # The V2 registry wraps the whole V1 data structure in the "data" field
    assert result_data.get("data", {}).get("projects") == {"proj-1": "id-1", "proj-2": "id-2"}

    # Verify projects.json was NOT moved to quarantine (it was a real V1 source, not stray)
    quarantine_dir = backups_dir / "stray-v1-sources"
    assert not quarantine_dir.exists(), "quarantine dir should not exist for fresh install"

    # Verify no quarantine entries in journal
    entries = journal.all_entries()
    quarantine_entries = [e for e in entries if e.action == "quarantine"]
    assert len(quarantine_entries) == 0, "no quarantine should happen on fresh install"


def test_doctor_shows_stray_v1_sources_in_quarantine(tmp_path):
    """#636 Test 5: takkub doctor --storage-layout shows files in quarantine.
    This test verifies that the _stray_v1_source_findings() function
    (used by doctor --storage-layout) correctly detects and reports
    quarantined stray V1 source files."""
    from unittest.mock import patch

    from agent_takkub.doctor import Status, _stray_v1_source_findings

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Create quarantined stray files
    quarantine_dir = backups_dir / "stray-v1-sources" / "1234_5678"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    (quarantine_dir / "projects.json").write_text("{}", encoding="utf-8")
    (quarantine_dir / "custom-roles.json").write_text("{}", encoding="utf-8")

    # Patch BackupManager at the import location inside the function
    with patch("agent_takkub.core.migration.backup.BackupManager") as mock_backup_mgr:
        mock_instance = mock_backup_mgr.return_value
        mock_instance.root = backups_dir

        # Call the doctor function
        findings = _stray_v1_source_findings()

        # Verify it found the stray files
        assert len(findings) == 1
        finding = findings[0]
        assert finding.category == "storage-layout"
        assert finding.name == "stray-v1-sources"
        assert finding.status == Status.WARN
        assert "2 stray V1 source file(s) quarantined" in finding.detail
        assert "projects.json" in finding.detail
        assert "custom-roles.json" in finding.detail


def test_doctor_no_findings_when_no_stray_files(tmp_path):
    """#636 Test 5b: doctor returns no findings when no stray files
    are in quarantine (normal case after cleanup)."""
    from unittest.mock import patch

    from agent_takkub.doctor import _stray_v1_source_findings

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # No quarantine directory = no stray files
    with patch("agent_takkub.core.migration.backup.BackupManager") as mock_backup_mgr:
        mock_instance = mock_backup_mgr.return_value
        mock_instance.root = backups_dir

        findings = _stray_v1_source_findings()

        # Should return empty list
        assert len(findings) == 0
