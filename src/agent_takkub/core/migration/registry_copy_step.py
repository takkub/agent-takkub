"""`RegistryCopyStep` — the shared shape behind plan §5.3 ladder steps 1, 3
and 5 (read-only registries / capability / state): each of those steps is
"copy N small V1 JSON files verbatim into their V2 layout location", nothing
more, so one generic step type backs all three instead of three near-
identical classes (steps 2/4/6/7 need real fan-out or reference-only
semantics and get their own step classes in `steps_v1.py`).

Full-fidelity passthrough by design: the V1 JSON is wrapped
(`{"schema", "migrated_from", "migrated_at", "data": <v1 json verbatim>}`)
rather than reshaped field-by-field, so "unknown field ห้ามทิ้งเงียบ" (plan
§5.1) holds trivially — nothing is dropped because nothing is restructured.
A later phase that actually needs the V2 domain shape (e.g. a real
`ModelRegistry`) reads `data` out of this wrapper, same as
`core.storage.legacy_reader` already does for its own V1 readers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..storage.legacy_reader import read_json
from .backup import BackupManager
from .journal import MigrationJournal
from .report import StepReport


@dataclass(frozen=True, slots=True)
class RegistryMapping:
    name: str
    source: Path
    target: Path
    note: str = ""


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class RegistryCopyStep:
    step_id: str
    mappings: tuple[RegistryMapping, ...]
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)

    def _backup_key(self, mapping: RegistryMapping) -> str:
        # Composite key, not bare step_id: two mappings in the same step can
        # legitimately target the same basename in different V2 subdirs
        # (e.g. models/registry.json AND providers/registry.json both end
        # in "registry.json") — `BackupManager.latest_backup` only keys by
        # basename within a step_id folder, so a bare step_id would let one
        # mapping's backup shadow another's.
        return f"{self.step_id}__{mapping.name}"

    def inspect(self) -> StepReport:
        present = [m.name for m in self.mappings if m.source.exists()]
        missing = [m.name for m in self.mappings if not m.source.exists()]
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(present)}/{len(self.mappings)} V1 source(s) present",
            detail={"present": present, "missing": missing},
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will write {len(self.mappings)} target(s) under V2 layout (copy-never-move)",
            detail={m.name: str(m.target) for m in self.mappings},
        )

    def dry_run(self) -> StepReport:
        would_change = [
            m.name for m in self.mappings if read_json(m.target).get("data") != read_json(m.source)
        ]
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; {len(would_change)}/{len(self.mappings)} target(s) would change",
            detail={"would_change": would_change},
        )

    def apply(self) -> StepReport:
        written: list[str] = []
        for m in self.mappings:
            self.backups.backup(self._backup_key(m), m.target)
            payload = {
                "schema": 1,
                "migrated_from": str(m.source),
                "migrated_at": time.time(),
                "data": read_json(m.source),
            }
            try:
                write_json_atomic(m.target, payload)
            except OSError as e:
                self.journal.record(self.step_id, "apply", False, f"{m.name}: {e}")
                return StepReport(self.step_id, "apply", False, f"write failed for {m.name}: {e}")
            written.append(m.name)
        self.journal.record(self.step_id, "apply", True, f"wrote {len(written)} target(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"wrote {len(written)} target(s)",
            detail={"written": written},
        )

    def validate(self) -> StepReport:
        mismatched = [
            m.name for m in self.mappings if read_json(m.target).get("data") != read_json(m.source)
        ]
        ok = not mismatched
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "all targets match V1 source" if ok else f"{len(mismatched)} target(s) mismatched",
            detail={"mismatched": mismatched},
        )

    def rollback(self) -> StepReport:
        restored: list[str] = []
        for m in self.mappings:
            backup = self.backups.latest_backup(self._backup_key(m), m.target.name)
            try:
                if backup is None:
                    m.target.unlink(missing_ok=True)
                else:
                    self.backups.restore(backup, m.target)
            except OSError as e:
                self.journal.record(self.step_id, "rollback", False, f"{m.name}: {e}")
                return StepReport(
                    self.step_id, "rollback", False, f"restore failed for {m.name}: {e}"
                )
            restored.append(m.name)
        self.journal.record(self.step_id, "rollback", True, f"restored {len(restored)} target(s)")
        return StepReport(
            self.step_id,
            "rollback",
            True,
            f"restored {len(restored)} target(s)",
            detail={"restored": restored},
        )
