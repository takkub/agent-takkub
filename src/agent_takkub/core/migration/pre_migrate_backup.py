"""`PreMigrateBackupStep` (#574) — a copy-only, copy-verified snapshot of
every V1 artifact the rest of the ladder might OVERWRITE, MERGE INTO, or
DELETE OUTRIGHT, taken BEFORE any other step runs. Ladder position: first,
ahead of even `version-marker` — `MigrationEngine.apply()` already stops at
the first failing step, so putting this step first is what makes "a failed
backup aborts the whole ladder before anything else is touched" true for
free on THAT path, with no extra logic needed here. `apply_pending()`'s own
"No stop-the-line" contract does NOT get this for free (#504/#574 R8-H1) —
it carries one explicit exception for this step's id instead, right next to
the `promote-v2-root` one it already had.

#574 round11 item 1 (SCOPE): this used to ALSO back up every pure-move
candidate (every `ArchiveV1LegacyStep` archive/shared-dir-legacy item, every
non-colliding `v2/*` promote candidate, and `runtime/core` wholesale) —
real cost on a production-sized store: a rehearsal on a ~200k-file copy of
prod stalled 30 minutes into phase 1/5, having only reached ~2.1 GB of a
store whose `claude-config`/`runtime` trees alone run into the tens of GB.
None of that redundancy was buying real protection: a MOVE (copy-verify via
`_copy_phase`'s own WAL, then prune only after that verify succeeds) is
already fully recoverable through `restore-v1`/`rollback()` without this
step's help — its cost scales with the WHOLE store instead of with what
this pass can actually damage. This step now backs up only:

1. a `v2/*` promote candidate whose top-level destination ALREADY holds
   content (`PromoteV2RootStep` is about to MERGE into it, not move into
   empty space — see `_promote_merge_entries()`);
2. each of the 7 domain steps' (readonly-registries/role-agent/capability/
   project/state/credential-reference/core-internal-store) own V2 targets,
   but ONLY the ones that already exist (a target that doesn't exist yet is
   a pure first write — nothing pre-existing to lose, see
   `_domain_backup_targets()`) — each of those steps already takes its OWN
   per-run `BackupManager` snapshot before writing, so this is a second,
   independent, never-rotated-away copy of the state THIS ladder pass
   actually started from, not the only copy;
3. `version-marker`'s own target (`version_doc_path()`), for the same
   belt-and-suspenders reason as 2 — `VersionMarkerStep.apply()` already
   takes its own backup too;
4. `ArchiveV1LegacyStep`'s `_delete_candidates()` — #504 item 5's named
   junk, which gets DELETED OUTRIGHT with no archive copy anywhere else,
   the one case in this whole ladder with no other recovery path at all.

Everything else (genuine archive candidates, shared-dir-legacy files,
non-colliding promote candidates, `runtime/core` wholesale) is a pure MOVE
already protected by its own step's copy-verify-then-prune WAL — this
step's own `_input_entries()` never touches them, and `plan()`/`inspect()`
detail explains why each is skipped so the wizard's PreMigrate screen can
show the full picture, not just what actually gets copied here.

Never deletes anything, never expires its own output (user directive via
#574's task brief) — `rollback()` is a no-op that reports ok without
touching the backup. Resumable: `_copy_phase` already gives file-level
resume-from-WAL for free; a `manifest.json` written on success additionally
lets a repeated `apply()` (a fresh v1-state `MigrationEngine.apply()` run,
which does not consult the journal that would otherwise skip an
already-applied step) recognize a complete prior backup and skip real work.
`_write_manifest()` merges with whatever a PRIOR run's manifest already
recorded rather than replacing it outright (#574 round11 item 4): an item
that was in scope under an OLDER version of this step but has since fallen
OUT of scope (a code upgrade narrowed `_input_entries()`, same as this very
round's own SCOPE change) is kept in the manifest flagged
``"out_of_scope": true`` — its already-copied backup files are never
deleted, and `_already_backed_up()` only ever requires the CURRENT
`_input_entries()` to be present, never the historical full set."""

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
from .promote_v1 import (
    _LEGACY_V2_NAME,
    ArchiveV1LegacyStep,
    PromoteV2RootStep,
    TransferEntry,
    _copy_phase,
    _rel_files,
    _verified_target_intact,
)
from .registry_copy_step import write_json_atomic
from .report import StepReport
from .wal import TransferLedger

STEP_ID = "pre-migrate-backup"

# #574 round11 item 1: bytes/files thresholds above which `plan()` surfaces
# a warning (text summary + JSON `detail.warning`) rather than silently
# proceeding — the wizard's PreMigrate screen and `takkub migrate plan
# --json` both read this the same way every other `StepReport.detail` key
# is read, no new plumbing needed.
_WARN_BYTES_THRESHOLD = 500 * 1024 * 1024
_WARN_FILES_THRESHOLD = 20_000

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
    # #574 round14 (Lead item 7, investigated, NOT relocated): a
    # 2.0.8 downgrade onto a restored store reports `pre-migrate-backup
    # -dir.txt`'s presence under `runtime/core` as a `core-internal-store`
    # mismatch (2.0.8 predates this marker and never excludes it the way
    # 2.1.0's own `OWN_BOOKKEEPING_NAMES`/`_excluded_names()` do) — moving
    # it OUTSIDE `runtime/core` (`backups/`, DATA_HOME-top-level) would
    # fix that, but `resolve_backup_dir()` below PERSISTS a marker (and,
    # at this new location, materializes `backups/` itself) as a side
    # effect of merely being CALLED — including from `_input_entries()`/
    # `validate()`'s own read-only path computation, reached even on a
    # genuinely fresh machine with nothing to back up
    # (`test_fresh_data_home_boot_gets_the_new_layout_with_no_archive`,
    # `test_fresh_boot_state_file_does_not_get_archived_on_the_second_boot`
    # both caught this: moving the marker made `backups/` appear on a
    # fresh install with zero backup activity). Fixing that needs
    # `resolve_backup_dir()` itself to stop writing on a read-only call —
    # a larger, riskier change than this LOW-priority item's own budget;
    # left at the pre-round14 location, unresolved, for a dedicated pass.
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


def _version_marker_path(data_home: Path) -> Path:
    """Mirrors `core.versioning.store.version_doc_path()`'s own
    `core_home()`-fallback logic, but rooted at *data_home* explicitly
    rather than the global `config.DATA_HOME`/`config.RUNTIME_DIR` that
    function actually reads. The two are always the SAME path in
    production (`MigrationEngine()`'s default construction always passes
    `data_home=config.DATA_HOME`) — this exists so a caller with an
    explicit, decoupled `data_home` (every test in this package) gets a
    path guaranteed to resolve UNDER that `data_home`, so its
    `.relative_to(data_home)` (used as this entry's manifest `name`, see
    `_input_entries()`) never raises and never points at the real
    machine's own version.json."""
    from ..storage.layout import storage_layout_v2

    system = storage_layout_v2(data_home).system
    if system.is_dir():
        return system / "version.json"
    return data_home / "runtime" / "core" / "version.json"


def _domain_backup_targets(data_home: Path) -> list[Path]:
    """Every V2-layout target path the 7 domain steps named in #574
    round11 item 1 might overwrite or merge into — each one built from
    `storage_layout_v2(data_home)` (directly, or via a throwaway step
    instance's own `data_home=` field), so it's always resolvable relative
    to *data_home* — the same relative path `_input_entries()` uses as
    this entry's manifest `name`, which `restore_from_backup_dir()` later
    reconstructs a destination from as `data_home / name`. The step
    objects here are throwaway: constructed only to read a path-computing
    accessor, never given a journal/backups instance, never `apply()`ed."""
    from .steps_v1 import (
        CoreInternalStoreStep,
        CredentialReferenceStep,
        ProjectMigrationStep,
        RoleAgentMigrationStep,
        build_capability_step,
        build_readonly_registries_step,
        build_state_step,
    )

    out: list[Path] = []
    for builder in (build_readonly_registries_step, build_capability_step, build_state_step):
        out.extend(m.target for m in builder(data_home=data_home).mappings)

    role_agent = RoleAgentMigrationStep(data_home=data_home)
    out.append(role_agent._custom_roles_target())
    out.append(role_agent._routing_target())

    project = ProjectMigrationStep(data_home=data_home)
    out.append(project._registry_target())
    for project_id in project._load().get("projects", {}):
        out.append(project._project_target(project_id))

    creds = CredentialReferenceStep(data_home=data_home)
    for provider in creds._refs():
        out.append(creds._provider_target(provider))
        out.append(creds._account_target(provider))

    out.append(CoreInternalStoreStep(data_home=data_home)._target())
    return out


def _entry_bytes(entry: TransferEntry) -> int:
    try:
        if entry.kind == "file":
            return entry.src.stat().st_size
        return sum(
            (entry.src / rel).stat().st_size for rel in entry.paths if (entry.src / rel).is_file()
        )
    except OSError:
        return 0


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
    # Best-effort WITHIN-entry progress observer (#574 round11 item 3) —
    # `(entry_name, files_done, files_total, current_path)`, forwarded
    # into `_copy_phase`'s own `on_file_progress` — the exact signal that
    # was missing during the real ~200k-file rehearsal that sat silent on
    # one entry for 17+ minutes.
    on_file_progress: Callable[[str, int, int, str], None] | None = None

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
        """#574 round11 item 1 (SCOPE): only what `ArchiveV1LegacyStep`/
        `PromoteV2RootStep`/the 7 domain steps might OVERWRITE, MERGE INTO,
        or DELETE OUTRIGHT this same pass — see this module's own docstring
        for the full reasoning and `skipped_move_only_items()` for the
        (much larger) set of pure-move items deliberately left OUT."""
        home = self.data_home
        backup_dir = self._backup_dir()
        entries: list[TransferEntry] = []
        seen: set[str] = set()

        def add(label: str, src: Path) -> None:
            if label in seen or not src.exists():
                return
            seen.add(label)
            if src.is_dir():
                paths = tuple(p.as_posix() for p in _rel_files(src))
                if not paths:
                    return  # genuinely empty — nothing worth backing up
                entries.append(TransferEntry(label, "dir", src, backup_dir / label, paths))
            else:
                entries.append(TransferEntry(label, "file", src, backup_dir / label))

        # 1) promote-v2-root MERGE collisions only — a `v2/*` candidate
        # whose top-level destination already exists (`PromoteV2RootStep`
        # is about to merge into it). A NON-colliding candidate is a pure
        # move into empty space, already protected by that step's own
        # copy-verify-then-prune WAL. Labeled by its own bare top-level
        # name (never a synthetic prefix) — `restore_from_backup_dir()`
        # reconstructs `data_home / name` from this later, so it must be
        # the item's real data_home-relative location.
        promote = PromoteV2RootStep(data_home=home)
        for p in promote._promote_candidates():
            target = home / p.name
            if target.exists():
                add(p.name, target)

        # 2) each of the 7 domain steps' own V2 targets — ONLY the ones
        # that already hold pre-existing content (an absent target is a
        # pure first write, nothing to lose). Every target from
        # `_domain_backup_targets()` is built under `home`, so
        # `.relative_to(home)` never raises.
        for target in _domain_backup_targets(home):
            add(target.relative_to(home).as_posix(), target)

        # 3) version-marker's own target — small, cheap, belt-and-
        # suspenders alongside `VersionMarkerStep`'s own per-run backup.
        marker = _version_marker_path(home)
        add(marker.relative_to(home).as_posix(), marker)

        # 4) #504 item 5's named junk — deleted OUTRIGHT by
        # `ArchiveV1LegacyStep` with no archive copy anywhere else, the one
        # case in this whole ladder with no other recovery path.
        archive = ArchiveV1LegacyStep(data_home=home)
        for p in archive._delete_candidates():
            add(p.name, p)

        return entries

    def skipped_move_only_items(self) -> list[tuple[str, str]]:
        """``(label, reason)`` for every candidate `_input_entries()`
        deliberately leaves OUT because it's a pure move already protected
        by its own step's copy-verify-then-prune WAL (+ `restore-v1`/
        `rollback()`) — surfaced in `inspect()`/`plan()` detail so the
        wizard's PreMigrate screen can show the full picture instead of
        silently doing less than a reader might expect from the old,
        wider-scope backup."""
        home = self.data_home
        reason_moved = (
            "moved (copy-verify then prune), not copied here — its own WAL + "
            "restore-v1/rollback already protect it"
        )
        out: list[tuple[str, str]] = []
        if (home / _LEGACY_V2_NAME).is_dir():
            promote = PromoteV2RootStep(data_home=home)
            for p in promote._promote_candidates():
                if not (home / p.name).exists():
                    out.append((f"v2/{p.name}", reason_moved))
        archive = ArchiveV1LegacyStep(data_home=home)
        for p in archive._archive_candidates():
            out.append((p.name, reason_moved))
        for p in archive._shared_dir_legacy_candidates():
            out.append((p.relative_to(home).as_posix(), reason_moved))
        return out

    def _pending(self) -> bool:
        entries = self._input_entries()
        return bool(entries) and not self._already_backed_up(entries)

    def _already_backed_up(self, entries: list[TransferEntry]) -> bool:
        """#574 round11 item 4: `done` only ever counts an item the manifest
        recorded as genuinely verified UNDER THE CURRENT `_input_entries()`
        scope — never a name kept only because an OLDER manifest happened
        to have it (`out_of_scope: true`, see `_write_manifest()`), and
        never a requirement that `entries` match some historical full set.
        A version upgrade that narrows or widens scope compares cleanly
        either way: fewer/different names than a prior manifest still
        resolves correctly here, no assumption of a stable entry set
        across versions.

        #504/#574 R8-H2: a manifest NAME match alone used to be enough —
        delete one recorded payload out from under it and `apply()` still
        reported "already backed up (resumed)" without ever re-copying it,
        the ladder then walking on with one fewer file actually protected
        than the manifest claimed. Every entry's OWN recorded `sha256` is
        now recomputed against `backup_dir`'s CURRENT on-disk content (the
        exact same `_verified_target_intact` resume check `promote_v1`'s
        own copy/prune WAL already trusts) — a missing file or a
        content mismatch means this entry is NOT actually backed up,
        triggering a real re-copy rather than a false "resumed"."""
        manifest = self._existing_manifest()
        if manifest is None:
            return False
        done = {
            item["name"]: item
            for item in manifest.get("items", [])
            if not item.get("out_of_scope") and item.get("name")
        }
        # `e.dest` is already `backup_dir / e.name` — `_input_entries()`
        # builds every entry that way — so `_verified_target_intact` reads
        # back exactly what a real backup would have written there.
        return all(e.name in done and _verified_target_intact(e, done[e.name]) for e in entries)

    def inspect(self) -> StepReport:
        entries = self._input_entries()
        skipped = self.skipped_move_only_items()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(entries)} item(s) would be backed up to {self._backup_dir()}; "
            f"{len(skipped)} move-only item(s) skipped (already protected elsewhere)",
            detail={
                "items": [e.name for e in entries],
                "skipped_move_only": [{"name": n, "reason": r} for n, r in skipped],
            },
        )

    def plan(self) -> StepReport:
        entries = self._input_entries()
        total_files = sum(len(e.paths) if e.kind == "dir" else 1 for e in entries)
        total_bytes = sum(_entry_bytes(e) for e in entries)
        summary = (
            f"will copy-verify {len(entries)} item(s) ({total_files} file(s), "
            f"{total_bytes / (1024 * 1024):.1f} MB) into {self._backup_dir()} before "
            "anything else in the ladder runs"
        )
        detail: dict = {"estimated_files": total_files, "estimated_bytes": total_bytes}
        # #574 round11 item 1: surface a warning above these thresholds
        # rather than silently proceeding — in BOTH the text summary and
        # `detail` (so `takkub migrate plan --json` and the wizard's
        # PreMigrate screen see it the same way).
        if total_bytes > _WARN_BYTES_THRESHOLD or total_files > _WARN_FILES_THRESHOLD:
            warning = (
                f"backup covers {total_files} file(s) / "
                f"{total_bytes / (1024 * 1024):.0f} MB — this may take a while"
            )
            detail["warning"] = warning
            summary += f" — WARNING: {warning}"
        return StepReport(self.step_id, "plan", True, summary, detail=detail)

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
            # #574 round11 item 4: reconcile the manifest even on this
            # fast path — a resumed run under a NARROWED scope (a code
            # upgrade since the prior full apply) must still flag whatever
            # fell out of scope, not just skip re-copying and leave the
            # manifest exactly as the OLDER, wider-scope code left it. No
            # `digests` for `entries` here (nothing was just copied) —
            # `_write_manifest` falls back to each name's already-recorded
            # digest from the existing manifest.
            manifest_error = self._write_manifest(entries, {})
            if manifest_error is not None:
                msg = (
                    f"already backed up at {self._backup_dir()} but manifest reconcile "
                    f"failed: {manifest_error}"
                )
                self.journal.record(self.step_id, "apply", False, msg)
                return StepReport(self.step_id, "apply", False, msg)
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
        outcome = _copy_phase(
            entries,
            self.backups,
            self.step_id,
            ledger,
            on_entry=self._notify,
            on_file_progress=self.on_file_progress,
        )
        if not outcome.ok:
            self.journal.record(self.step_id, "apply", False, outcome.error)
            return StepReport(
                self.step_id, "apply", False, f"pre-migrate backup failed: {outcome.error}"
            )
        manifest_error = self._write_manifest(entries, outcome.digests)
        if manifest_error is not None:
            # #574 round11 R7-H3: every file above is already durably
            # copy-verified — the WAL is deliberately left UNCLEARED, so a
            # retry finds the already-verified work and only needs to
            # rewrite the manifest, never re-copy anything. But with no
            # manifest, `validate()`/`restore_from_backup_dir()` have no
            # ownership record to read back: report this as a real
            # failure (stopping the ladder here) rather than `ok=True`
            # over an escape hatch that silently doesn't work.
            msg = (
                f"backed up {len(entries)} item(s) to {self._backup_dir()} but manifest write "
                f"failed, stopping: {manifest_error}"
            )
            self.journal.record(self.step_id, "apply", False, msg)
            return StepReport(self.step_id, "apply", False, msg)
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
    ) -> str | None:
        """Returns an error message on failure (never raises) — `None` on
        success. #574 round11 item 4: merges with whatever a PRIOR run's
        manifest already recorded rather than replacing it outright — an
        item that WAS in scope under an older version of `_input_entries()`
        but has since fallen out of scope is kept, flagged
        ``out_of_scope: true``, never silently dropped from the record even
        though its already-copied backup files are left exactly where they
        are (never deleted, per this step's own "keep forever" contract).

        *digests* need not cover every name in *entries* — the
        already-backed-up (resumed) fast path in `apply()` calls this with
        `{}` purely to reconcile out-of-scope flags, nothing just copied;
        a name missing from *digests* falls back to whatever the PRIOR
        manifest already recorded for it, never silently reset to `{}`."""
        prior = self._existing_manifest() or {}
        prior_by_name = {
            item["name"]: item
            for item in prior.get("items", [])
            if isinstance(item, dict) and item.get("name")
        }
        new_names = {e.name for e in entries}
        kept_out_of_scope = [
            {**item, "out_of_scope": True}
            for name, item in prior_by_name.items()
            if name not in new_names
        ]
        payload = {
            "schema": 2,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "backup_dir": str(self._backup_dir()),
            "items": [
                {
                    "name": e.name,
                    "kind": e.kind,
                    "src": str(e.src),
                    "paths": list(e.paths),
                    "sha256": digests.get(e.name)
                    or prior_by_name.get(e.name, {}).get("sha256", {}),
                }
                for e in entries
            ]
            + kept_out_of_scope,
        }
        path = self._manifest_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, payload)
        except OSError as e:
            return str(e)
        return None

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
        backup_dir = self._backup_dir()
        manifest = self._existing_manifest()
        if manifest is None:
            # #574 round11 R7-H3: a backup dir that already has CONTENT but
            # no readable manifest is a real failure (the write that should
            # have recorded ownership over it never landed) — never read as
            # "nothing to check" just because apply() looks like it hasn't
            # run yet. An empty/absent backup dir is the genuine "hasn't
            # run yet" case this used to unconditionally assume.
            try:
                has_content = backup_dir.is_dir() and any(backup_dir.iterdir())
            except OSError:
                has_content = False
            if has_content:
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"backup dir {backup_dir} has content but its manifest is missing or unreadable",
                )
            return StepReport(self.step_id, "validate", True, "no pre-migrate backup manifest yet")
        items = manifest.get("items", [])
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


def restore_backed_up_items(backup_dir: Path, data_home: Path) -> StepReport:
    """`takkub migrate restore-v1`'s own missing half (#504/#574 item 13;
    generalized round14 R9-H1). The normal restore-v1 path
    (`archive_step.rollback()` + `promote_step.rollback()`) only reverses
    what THOSE two steps themselves wrote — it has no way to bring back a
    DOMAIN step's own V2-target overwrite of a pre-existing V1 file
    (`readonly-registries` writing its own envelope over an existing
    `models/registry.json`, say — R9-H1's own repro), nor #504 item 5's
    named junk (deleted OUTRIGHT by `archive-v1-legacy`, never archived at
    all). This step's own manifest (item 4 of `_input_entries()`'s
    docstring) is the ONE place a copy of ANY of that survives — restore
    EVERY item it recorded, copy-only, from *backup_dir*'s CURRENT
    manifest, alongside the normal archive/promote rollback (never
    instead of it: a genuine pure-move item promote/archive already
    restores correctly via their own WAL, and re-copying the SAME
    pre-migrate snapshot over it here is harmless — both agree
    byte-for-byte on a successful restore). A missing/unreadable
    manifest, or a manifest with no items, is a normal no-op — most
    stores never had any, or never took a pre-migrate backup at all (a
    machine on an older release)."""
    manifest_path = backup_dir / _MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StepReport(
            STEP_ID, "restore", True, f"no pre-migrate backup manifest at {manifest_path}"
        )
    items = [item for item in manifest.get("items", []) if item.get("name")]
    if not items:
        return StepReport(STEP_ID, "restore", True, "no backed-up item(s) to restore")
    entries = [
        TransferEntry(
            item["name"],
            item.get("kind", "file"),
            backup_dir / item["name"],
            data_home / item["name"],
            tuple(item.get("paths", ())),
        )
        for item in items
    ]
    ledger = TransferLedger(
        migration_home() / "pre-migrate-restore-items-wal.json", write_fn=write_json_atomic
    )
    outcome = _copy_phase(entries, BackupManager(), STEP_ID, ledger)
    ledger.clear()
    if not outcome.ok:
        return StepReport(STEP_ID, "restore", False, f"restore failed: {outcome.error}")
    return StepReport(
        STEP_ID,
        "restore",
        True,
        f"restored {len(entries)} backed-up item(s) from {backup_dir}",
        detail={"items": [e.name for e in entries]},
    )


__all__ = [
    "STEP_ID",
    "PreMigrateBackupStep",
    "resolve_backup_dir",
    "restore_backed_up_items",
    "restore_from_backup_dir",
]
