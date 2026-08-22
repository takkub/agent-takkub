"""Core V2 migration engine: journal, copy-never-move backups, the
VersionMarkerStep proof-of-pipeline step, and MigrationEngine's
stop-the-line semantics (#309 Phase 4)."""

from __future__ import annotations

import json

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
    """The default ladder (#309 Phase 8b, plan §5.3) is version-marker + the
    7 V1->V2 steps, in risk order — version-marker stays first since it
    predates the ladder and other code (doctor) depends on it running."""
    monkeypatch.setattr(
        "agent_takkub.core.migration.steps.version_doc_path", lambda: tmp_path / "version.json"
    )
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path / "settings_home")
    engine = MigrationEngine()
    reports = engine.inspect()
    assert len(reports) == 8
    assert reports[0].step_id == "version-marker"
    assert [r.step_id for r in reports[1:]] == [
        "readonly-registries",
        "role-agent",
        "capability",
        "project",
        "state",
        "credential-reference",
        "runtime-triage",
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


def test_engine_inspect_plan_dry_run_always_run_every_step():
    a = _FakeStep("a", ok=False)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    assert len(engine.inspect()) == 2
    assert len(engine.plan()) == 2
    assert len(engine.dry_run()) == 2


def test_engine_rollback_runs_in_reverse_order():
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=True)
    engine = MigrationEngine([a, b])
    engine.rollback()
    assert a.calls == ["rollback"]
    assert b.calls == ["rollback"]


def test_engine_rollback_stops_on_first_failure_in_reverse_order():
    a = _FakeStep("a", ok=True)
    b = _FakeStep("b", ok=False)
    engine = MigrationEngine([a, b])
    reports = engine.rollback()
    assert len(reports) == 1
    assert reports[0].step_id == "b"
    assert a.calls == []  # never reached, b failed first in reverse order


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
