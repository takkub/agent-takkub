"""Core V2 migration engine: journal, copy-never-move backups, the
VersionMarkerStep proof-of-pipeline step, and MigrationEngine's
stop-the-line semantics (#309 Phase 4)."""

from __future__ import annotations

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
