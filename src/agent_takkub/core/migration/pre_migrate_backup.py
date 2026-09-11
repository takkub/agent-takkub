"""`PreMigrateBackupStep` (#574) — a copy-only, copy-verified snapshot of
every V1 artifact the rest of the ladder is about to touch, taken BEFORE any
other step runs. Ladder position: first, ahead of even `version-marker` —
`MigrationEngine.apply()`/`apply_pending()` already stop at the first
failing step, so putting this step first is what makes "a failed backup
aborts the whole ladder before anything else is touched" true for free,
with no new stop-the-line logic needed here.

Backs up: the legacy nested `v2/` root (`PromoteV2RootStep`'s own input),
every `ArchiveV1LegacyStep` archive/shared-dir-legacy candidate, and —
separately, since `ArchiveV1LegacyStep` explicitly PROTECTS them —
`projects/` and `agents/`, which the domain steps are about to WRITE into
this same pass. `runtime/core` (`CoreInternalStoreStep`'s own source) is
included too even though nothing prunes it, purely as an extra safety copy.

Never deletes anything, never expires its own output (user directive via
#574's task brief) — `rollback()` is a no-op that reports ok without
touching the backup. Resumable: `_copy_phase` already gives file-level
resume-from-WAL for free; a `manifest.json` written on success additionally
lets a repeated `apply()` (a fresh v1-state `MigrationEngine.apply()` run,
which does not consult the journal that would otherwise skip an
already-applied step) recognize a complete prior backup and skip real work.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_takkub import config

from ..storage.paths import migration_home
from .backup import BackupManager
from .journal import MigrationJournal
from .promote_v1 import _LEGACY_V2_NAME, ArchiveV1LegacyStep, TransferEntry, _copy_phase, _rel_files
from .registry_copy_step import write_json_atomic
from .report import StepReport
from .wal import TransferLedger

STEP_ID = "pre-migrate-backup"

# Extra top-level names to back up on top of whatever `ArchiveV1LegacyStep`
# is about to touch — both are on `_ARCHIVE_SKIP_NAMES` (never archived) but
# the domain steps running later THIS SAME pass write into them.
_EXTRA_BACKUP_NAMES = ("projects", "agents")

_MARKER_NAME = "pre-migrate-backup-dir.txt"
_WAL_NAME = "pre-migrate-backup-wal.json"
_MANIFEST_NAME = "manifest.json"

# This step's own bookkeeping files live under `migration_home()`
# (RUNTIME_DIR/core) — the SAME directory its "runtime/core" input entry
# backs up wholesale. Both must be excluded from that entry's file list (or
# the backup would try to include its own still-being-written marker/WAL,
# growing self-referentially) AND from `CoreInternalStoreStep`'s later copy
# of `runtime/core` into `system/` (`steps_v1.py`'s own `_excluded_names()`
# — same exclusion list `MigrationJournal`/`BackupManager`'s files are
# already on, for the identical reason).
OWN_BOOKKEEPING_NAMES: frozenset[str] = frozenset({_MARKER_NAME, _WAL_NAME})


def _log_event(event: str, **details: object) -> None:
    """Best-effort structured log — mirrors `promote_v1._log_event`/`wal
    ._log_event` (kept as a separate, tiny copy here rather than imported,
    same reasoning: this module must stay importable/callable from a plain
    headless test with no orchestrator wiring at all)."""
    try:
        from ...orchestrator_text import _log_event as _emit

        _emit(event, **details)
    except Exception:
        return  # swallow-ok: this IS the fallback logging path itself — no
        # further sink to report its own failure to, and it never mutates
        # anything.


def _marker_path() -> Path:
    return migration_home() / _MARKER_NAME


def resolve_backup_dir(data_home: Path) -> Path:
    """The `backups/pre-migrate-<ts>/` directory this run's backup lives (or
    will live) at — stable across a boot-to-boot resume via a marker file at
    a fixed location (mirrors `PromoteV2RootStep`'s own fixed-WAL-path
    resume pattern), so a crashed mid-backup attempt reuses the SAME
    destination the next boot instead of starting a fresh, disconnected
    timestamped copy."""
    marker = _marker_path()
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    if name:
        return data_home / "backups" / name
    name = f"pre-migrate-{time.strftime('%Y-%m-%d-%H%M')}"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(name, encoding="utf-8")
    except OSError as e:
        # Worst case a retry picks a new timestamp next call; the backup
        # itself still succeeds, just under a fresh name — logged (not a
        # bare swallow), never escalated.
        _log_event("migration_pre_migrate_backup_marker_write_failed", error=str(e))
    return data_home / "backups" / name


@dataclass
class PreMigrateBackupStep:
    step_id: str = STEP_ID
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)
    # Best-effort per-entry progress observer (#574) — called with each
    # entry's `name` once its copy is durably verified (or recognized as
    # already-resumed-complete). Exceptions from it are swallowed; it can
    # never affect whether the backup itself succeeds.
    on_entry: Callable[[str], None] | None = None

    def _backup_dir(self) -> Path:
        return resolve_backup_dir(self.data_home)

    def _wal_path(self) -> Path:
        return migration_home() / _WAL_NAME

    def _manifest_path(self) -> Path:
        return self._backup_dir() / _MANIFEST_NAME

    def _existing_manifest(self) -> dict | None:
        try:
            data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # swallow-ok: missing/corrupt manifest reads as
            # "nothing backed up yet" — the same fail-open contract every
            # other small state file in this codebase already uses.
        return data if isinstance(data, dict) else None

    def _input_entries(self) -> list[TransferEntry]:
        home = self.data_home
        backup_dir = self._backup_dir()
        entries: list[TransferEntry] = []
        seen: set[str] = set()

        def add(label: str, src: Path, *, exclude_paths: frozenset[str] = frozenset()) -> None:
            if label in seen or not src.exists():
                return
            seen.add(label)
            if src.is_dir():
                paths = tuple(
                    p.as_posix() for p in _rel_files(src) if p.as_posix() not in exclude_paths
                )
                if not paths:
                    # Nothing left worth backing up here (either genuinely
                    # empty, or — `runtime/core` specifically — holding only
                    # this step's OWN marker/WAL, which `resolve_backup_dir()`
                    # creates as a side effect of merely being asked for a
                    # path, even during a read-only inspect()/plan()).
                    return
                entries.append(TransferEntry(label, "dir", src, backup_dir / label, paths))
            else:
                entries.append(TransferEntry(label, "file", src, backup_dir / label))

        add(_LEGACY_V2_NAME, home / _LEGACY_V2_NAME)
        archive = ArchiveV1LegacyStep(data_home=home)
        for p in archive._archive_candidates():
            add(p.name, p)
        for p in archive._shared_dir_legacy_candidates():
            add(p.relative_to(home).as_posix(), p)
        for name in _EXTRA_BACKUP_NAMES:
            add(name, home / name)
        # Exclude THIS step's own marker/WAL files (see
        # `OWN_BOOKKEEPING_NAMES`'s docstring) — never treat them as
        # migration-worthy content just because they happen to live
        # alongside it under `runtime/core`.
        add(
            "runtime/core",
            config.RUNTIME_DIR / "core",
            exclude_paths=OWN_BOOKKEEPING_NAMES,
        )
        return entries

    def _pending(self) -> bool:
        entries = self._input_entries()
        return bool(entries) and not self._already_backed_up(entries)

    def _already_backed_up(self, entries: list[TransferEntry]) -> bool:
        manifest = self._existing_manifest()
        if manifest is None:
            return False
        done = {item.get("name") for item in manifest.get("items", [])}
        return all(e.name in done for e in entries)

    def inspect(self) -> StepReport:
        entries = self._input_entries()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(entries)} item(s) would be backed up to {self._backup_dir()}",
            detail={"items": [e.name for e in entries]},
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will copy-verify every migration input into {self._backup_dir()} before "
            "anything else in the ladder runs",
        )

    def dry_run(self) -> StepReport:
        entries = self._input_entries()
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; {len(entries)} item(s) would be backed up",
            detail={"items": [e.name for e in entries]},
        )

    def _notify(self, name: str) -> None:
        if self.on_entry is None:
            return
        try:
            self.on_entry(name)
        except Exception:
            return  # swallow-ok: an observer failure must never fail the backup.

    def apply(self) -> StepReport:
        entries = self._input_entries()
        if not entries:
            self.journal.record(self.step_id, "apply", True, "nothing to back up")
            return StepReport(self.step_id, "apply", True, "nothing needed backing up")
        if self._already_backed_up(entries):
            self.journal.record(self.step_id, "apply", True, "already backed up (resumed)")
            for e in entries:
                self._notify(e.name)
            return StepReport(
                self.step_id,
                "apply",
                True,
                f"already backed up at {self._backup_dir()} (resumed)",
                detail={"backup_dir": str(self._backup_dir())},
            )

        ledger = TransferLedger(self._wal_path(), write_fn=write_json_atomic)
        outcome = _copy_phase(entries, self.backups, self.step_id, ledger, on_entry=self._notify)
        if not outcome.ok:
            self.journal.record(self.step_id, "apply", False, outcome.error)
            return StepReport(
                self.step_id, "apply", False, f"pre-migrate backup failed: {outcome.error}"
            )
        self._write_manifest(entries, outcome.digests)
        ledger.clear()
        self.journal.record(
            self.step_id, "apply", True, f"backed up {len(entries)} item(s) to {self._backup_dir()}"
        )
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"backed up {len(entries)} item(s) to {self._backup_dir()}",
            detail={"backup_dir": str(self._backup_dir()), "items": [e.name for e in entries]},
        )

    def _write_manifest(
        self, entries: list[TransferEntry], digests: dict[str, dict[str, str]]
    ) -> None:
        payload = {
            "schema": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "backup_dir": str(self._backup_dir()),
            "items": [
                {
                    "name": e.name,
                    "kind": e.kind,
                    "src": str(e.src),
                    "paths": list(e.paths),
                    "sha256": digests.get(e.name, {}),
                }
                for e in entries
            ],
        }
        path = self._manifest_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, payload)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, f"manifest write failed: {e}")

    def validate(self) -> StepReport:
        """Checks the manifest's OWN recorded item list, never a fresh
        `_input_entries()` recomputation — the live V1 input set legitimately
        SHRINKS over the course of the SAME apply() pass as later domain
        steps consume/replace it (and `projects/`/`agents/` didn't exist yet
        when this step ran, but do by the time the whole pass finishes and
        `MigrationEngine._downgrade_on_health` re-validates every already-ok
        step). Comparing against a moving target made this permanently
        report false failures the instant anything else in the ladder ran
        after it — the manifest, written once at backup time, is the only
        stable thing to check against."""
        manifest = self._existing_manifest()
        if manifest is None:
            # Never re-derive "should something have been backed up?" from
            # LIVE disk state here — same drift problem this whole method
            # exists to avoid. `apply()`'s own report is already the
            # authority on whether backup was needed and succeeded; by the
            # time validate() runs independently (`_downgrade_on_health`,
            # `takkub migrate validate`, a later boot's `apply_pending`),
            # trust that and treat "no manifest yet" as simply nothing to
            # check here.
            return StepReport(self.step_id, "validate", True, "no pre-migrate backup manifest yet")
        items = manifest.get("items", [])
        backup_dir = self._backup_dir()
        missing = [item["name"] for item in items if not (backup_dir / item["name"]).exists()]
        if missing:
            return StepReport(
                self.step_id,
                "validate",
                False,
                f"backup manifest missing {len(missing)} item(s): {missing[:3]}",
            )
        return StepReport(
            self.step_id,
            "validate",
            True,
            f"{len(items)} item(s) backed up at {backup_dir}",
            detail={"backup_dir": str(backup_dir)},
        )

    def rollback(self) -> StepReport:
        # Permanent safety net, never undone by a ladder rollback (user
        # directive #574: "ไม่ลบเอง ไม่หมดอายุ") — a later step's failure
        # rolling the REST of the ladder back must still leave this backup
        # in place, untouched.
        self.journal.record(self.step_id, "rollback", True, "backup kept (never auto-deleted)")
        return StepReport(self.step_id, "rollback", True, "pre-migrate backup kept, not undone")

    def restore_into(self, data_home: Path | None = None) -> StepReport:
        """`takkub migrate restore-v1 --from-backup <dir>`'s mechanism
        (#574) for THIS step's own current backup dir — see
        `restore_from_backup_dir()` for the standalone version a caller
        naming an arbitrary backup directory uses instead."""
        target_home = data_home if data_home is not None else self.data_home
        return restore_from_backup_dir(self._backup_dir(), target_home, backups=self.backups)


def restore_from_backup_dir(
    backup_dir: Path, data_home: Path, *, backups: BackupManager | None = None
) -> StepReport:
    """Copy every item a `pre-migrate-backup` manifest at *backup_dir*
    recorded back onto *data_home*, copy-only — *backup_dir* itself is
    never modified or removed. A pre-existing destination is backed up
    first via the shared `BackupManager`, same as every other restore path
    in this package. Standalone (no `PreMigrateBackupStep` instance
    needed) so `takkub migrate restore-v1 --from-backup <dir>` can point at
    ANY such directory, not just the current one `resolve_backup_dir()`
    would compute."""
    manifest_path = backup_dir / _MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return StepReport(
            STEP_ID, "restore", False, f"could not read manifest at {manifest_path}: {e}"
        )
    entries = [
        TransferEntry(
            item["name"],
            item.get("kind", "dir"),
            backup_dir / item["name"],
            data_home / item["name"],
            tuple(item.get("paths", ())),
        )
        for item in manifest.get("items", [])
    ]
    ledger = TransferLedger(
        migration_home() / "pre-migrate-restore-wal.json", write_fn=write_json_atomic
    )
    outcome = _copy_phase(entries, backups or BackupManager(), STEP_ID, ledger)
    ledger.clear()
    if not outcome.ok:
        return StepReport(STEP_ID, "restore", False, f"restore failed: {outcome.error}")
    return StepReport(
        STEP_ID,
        "restore",
        True,
        f"restored {len(entries)} item(s) from {backup_dir} to {data_home}",
        detail={"items": [e.name for e in entries]},
    )


__all__ = ["STEP_ID", "PreMigrateBackupStep", "resolve_backup_dir", "restore_from_backup_dir"]
