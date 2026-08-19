"""Concrete `MigrationStep` implementations (structurally satisfy
`core.contracts.migration.MigrationStep` — no explicit inheritance needed,
mirrors `tests/test_core_contracts.py::_FakeMigrationStep`).

`VersionMarkerStep` is the Phase 4 proof-of-pipeline step: it does nothing
risky (it upserts `core/version.json`'s `"app"` component to the running
build's version) but exercises the full inspect → plan → dry_run → apply →
validate → rollback path end to end, backed by a real `BackupManager` +
`MigrationJournal` — so a later, riskier step from plan §5.3's ladder
(read-only registries → role/agent config → ... → credentials) can copy this
shape instead of inventing a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_takkub import __version__ as APP_VERSION

from ..versioning.store import read_version_doc, record_component, version_doc_path
from .backup import BackupManager
from .journal import MigrationJournal
from .report import StepReport


@dataclass
class VersionMarkerStep:
    step_id: str = "version-marker"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)

    def inspect(self) -> StepReport:
        path = version_doc_path()
        existing = read_version_doc(path)
        has_app = any(c.component == "app" for c in existing)
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"version.json {'exists' if path.exists() else 'missing'}; "
            f"app component {'present' if has_app else 'missing'}",
            detail={"path": str(path), "components": [c.component for c in existing]},
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will upsert component 'app' = {APP_VERSION!r} into {version_doc_path()}",
        )

    def dry_run(self) -> StepReport:
        path = version_doc_path()
        existing = {c.component: c.version for c in read_version_doc(path)}
        would_change = existing.get("app") != APP_VERSION
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; app component would {'change' if would_change else 'stay the same'}",
            detail={"current": existing.get("app"), "target": APP_VERSION},
        )

    def apply(self) -> StepReport:
        path = version_doc_path()
        self.backups.backup(self.step_id, path)
        try:
            record_component("app", APP_VERSION, path=path)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"write failed: {e}")
        self.journal.record(self.step_id, "apply", True, f"app -> {APP_VERSION}")
        return StepReport(self.step_id, "apply", True, f"wrote app component = {APP_VERSION}")

    def validate(self) -> StepReport:
        existing = {c.component: c.version for c in read_version_doc(version_doc_path())}
        ok = existing.get("app") == APP_VERSION
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "app component matches running build" if ok else "app component missing/mismatched",
        )

    def rollback(self) -> StepReport:
        path = version_doc_path()
        backup = self.backups.latest_backup(self.step_id, path.name)
        if backup is None:
            # Nothing existed before apply — rollback means "wasn't there".
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                self.journal.record(self.step_id, "rollback", False, str(e))
                return StepReport(self.step_id, "rollback", False, f"cleanup failed: {e}")
            self.journal.record(self.step_id, "rollback", True, "removed (no prior backup)")
            return StepReport(
                self.step_id, "rollback", True, "removed version.json (no prior state)"
            )
        self.backups.restore(backup, path)
        self.journal.record(self.step_id, "rollback", True, f"restored from {backup}")
        return StepReport(self.step_id, "rollback", True, "restored version.json from backup")
