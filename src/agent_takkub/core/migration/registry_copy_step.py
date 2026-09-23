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

from agent_takkub.cached_read import invalidate

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
    """Write *payload* to *path* via a tmp-file + `os.replace` (never a
    partial file observable at *path*). #504 round4 T2: also fsyncs the
    tmp file's content before the rename, and best-effort fsyncs the
    parent directory afterward — every caller of this function (migration
    ledgers/manifests included) needs the write to survive a hard crash,
    not just an unhandled exception, before this returns.

    Also drops *path* from `cached_read` (#658 contract: every in-process
    writer invalidates). Every reader of these targets goes through
    `legacy_reader.read_json`'s stat-free ≤3 s TTL, so without this the
    caller's own next read — a step's post-apply `validate()`, a Role
    Manager create's register/known_roles gate, `takkub mcp deny`'s
    variant regen + verify — saw the pre-write parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(tmp, "r+b") as f:
        os.fsync(f.fileno())
    os.replace(tmp, path)
    invalidate(path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return  # swallow-ok: directory-entry fsync is an extra durability
        # margin on top of the file fsync above (which already made the
        # content itself durable) — unsupported on some platforms (e.g.
        # opening a directory this way on Windows).


def _target_has_data(target: Path) -> bool:
    doc = read_json(target)
    if not isinstance(doc, dict):
        return False
    if bool(doc.get("data")):
        return True
    # Bare legacy dedup store written before #504 envelope wrap was restored
    if "fired" in doc or "signatures" in doc:
        return bool(doc.get("fired") or doc.get("signatures"))
    return False


@dataclass
class RegistryCopyStep:
    step_id: str
    mappings: tuple[RegistryMapping, ...]
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)

    def _mapping_retired(self, mapping: RegistryMapping) -> bool:
        return not mapping.source.exists() and _target_has_data(mapping.target)

    def source_retired(self) -> bool:
        """True once EVERY mapping's V1 source is gone AND its own target
        already holds real migrated data — #605: read by `MigrationEngine
        ._source_retired_for` as a per-step fallback when the ladder-wide
        `v1_retired` flag (driven by `ArchiveV1LegacyStep.validate()`) is
        false for an unrelated reason (e.g. OS junk clutter still sitting
        at DATA_HOME's top level). The "target already has data" half
        matters just as much as "source gone": a mapping whose V1 source
        never existed at all (nothing to migrate, not "already archived")
        must NOT be reported as retired before its own `apply()` has ever
        had a chance to write its (empty but valid) target — that used to
        make the engine skip this step's re-apply forever, leaving its
        target file never created at all (a real regression this fixed)."""
        return all(self._mapping_retired(m) for m in self.mappings)

    def stray_source_paths(self) -> list[Path]:
        """V1 source files that re-appeared after migration (#634) — if
        any mapping's source exists but its target already has data, it's
        a stray file that should be quarantined, not used as a source."""
        sources = []
        for m in self.mappings:
            if m.source.exists() and _target_has_data(m.target):
                sources.append(m.source)
        return sources

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
        kept: list[str] = []
        for m in self.mappings:
            if self._mapping_retired(m):
                # #605: the V1 source is already gone (archived by an
                # earlier pass) but the V2 target still holds real
                # migrated data — re-deriving from a missing source would
                # write `{"data": {}}` over it, wiping it out.
                # M1: still back up the already-correct target so
                # rollback always has something to restore instead of
                # deleting the "kept" data it exists to protect.
                self.backups.backup(self._backup_key(m), m.target)
                kept.append(m.name)
                continue
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
        summary = f"wrote {len(written)} target(s)"
        if kept:
            summary += f", kept {len(kept)} target(s) (V1 source archived)"
        self.journal.record(self.step_id, "apply", True, summary)
        return StepReport(
            self.step_id,
            "apply",
            True,
            summary,
            detail={"written": written, "kept": kept},
        )

    def validate(self) -> StepReport:
        mismatched = [
            m.name
            for m in self.mappings
            if not self._mapping_retired(m)
            and read_json(m.target).get("data") != read_json(m.source)
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
                    if not self._mapping_retired(m):
                        m.target.unlink(missing_ok=True)
                    # else: #605 M1 — a retired ("kept") target that was
                    # never backed up (e.g. backup dir pruned externally)
                    # must be preserved, not deleted; no-op. An empty
                    # `{"data": {}}` envelope is what apply itself writes for
                    # a source that never existed — that one IS undone.
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
