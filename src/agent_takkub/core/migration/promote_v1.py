"""#504 (2.1.0) — the one-shot "finish the move" pair appended to the end of
the existing V1->V2 ladder (#309/#361/#362), reusing exactly the same
`MigrationStep` protocol, `MigrationJournal`, and `BackupManager` every other
step already uses — no second engine, no new state machine.

Two steps, in ladder order, because they need V1 sources to still be on disk
at DIFFERENT points relative to the 8 existing V1->V2 steps:

* `PromoteV2RootStep` (ladder position 2, right after `version-marker`) —
  when a machine already has the pre-#504 nested ``DATA_HOME/v2/`` root
  (built by 2.0.x), move its contents up to `core.storage.layout
  .storage_layout_v2()`'s new top-level locations BEFORE any of the 8
  existing steps run their `validate()` in this same pass. Those steps'
  `validate()` now checks the SAME top-level paths `storage_layout_v2()`
  always resolves to (#504 item 6) — running promote first means they find
  their content already there (just relocated) and skip a redundant
  re-apply, instead of `apply_pending()` treating relocation as "went stale"
  and re-copying every domain fresh from V1 (harmless, since those steps are
  copy-never-move idempotent, but wasted work and a real risk of clobbering
  freshly-relocated content with an older nested-``v2/`` copy if the two
  ever raced). On a machine with no nested ``v2/`` (fresh install, or a box
  jumping straight from 1.x to 2.1.0 with the ladder never having run
  before) this is a no-op — nothing has been promoted-from yet.

* `ArchiveV1LegacyStep` (ladder position last, after `core-internal-store`)
  — archive whatever V1 top-level leftovers are STILL on disk into
  ``DATA_HOME/backups/v1-archive-<ts>/`` (never deleted — user directive
  2026-09-07: "ไม่ลบนะ ใช้การย้าย ... เผื่อเอากลับมาจะได้ไม่มีปัญหา"), delete
  the small named set of #504 item 5's "ศพเก่าที่ไม่ใช่ข้อมูล" outright, and
  archive the (by now empty) nested ``v2/`` folder too. Runs LAST precisely
  because every domain step above needs its V1 source still in place to
  read from — archiving first would starve every one of them.

Both moves use `verify_copy.copy_verified` (copy + sha256-verify every file,
THEN remove the original) — never a raw `shutil.move`, since DATA_HOME's
archive target may not share a volume with the source (#504 "ความปลอดภัยตอน
ย้าย"). Any failure mid-move triggers this step's OWN `_undo` before it
returns `ok=False`, so a partial move never lingers (#504: "ล้มกลางทาง →
journal rollback กลับสภาพเดิมทั้งก้อน ห้ามค้างครึ่ง") — on top of that,
`rollback()`/the CLI's `takkub migrate restore-v1` can always reconstruct the
pre-2.1.0 shape later from the archive's own manifest, since that archive is
never deleted or expired by this codebase.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_takkub import config

from .backup import BackupManager
from .journal import MigrationJournal
from .registry_copy_step import write_json_atomic
from .report import StepReport
from .verify_copy import VerifyMismatchError, copy_verified

_LEGACY_V2_NAME = "v2"
_ARCHIVE_DIR_NAME = "backups"
_MANIFEST_NAME = "manifest.json"

# Every top-level basename `core.storage.layout.storage_layout_v2()` itself
# ever computes — a domain step earlier in THIS SAME ladder run may have just
# created one of these moments before `ArchiveV1LegacyStep.apply()` runs
# (e.g. `project`/step 5 creates `projects/`, `role-agent`/step 2 creates
# `agents/custom/`), so none of these names may ever be archive candidates,
# full stop — the risk of destroying freshly-written V2 content outweighs
# fully cleaning up the handful of V1 artifacts that happen to share a
# directory name with a V2 one (`agents/<role>.md` sitting alongside V2's own
# `agents/custom/`; `SETTINGS_HOME/projects/<slug>/role-providers.json`
# alongside V2's own `projects/registry.json`) — those specific leftovers are
# already fully superseded content-wise (folded into `config/routing.json` /
# `agents/custom/registry.json` by the `role-agent` step), just not swept
# into the archive by this one. ponytail: surgical per-file archival inside
# a shared directory name is a real gap, not a guess — safe-by-construction
# beats a false-positive that deletes-by-move a directory a step just wrote.
_V2_TOP_LEVEL_NAMES: frozenset[str] = frozenset(
    {
        "config",
        "providers",
        "accounts",
        "models",
        "capabilities",
        "agents",
        "projects",
        "brain",
        "state",
        "runtime",
        "cache",
        "secrets",
        "system",
    }
)

# #504 item 8 "ของที่ไม่แตะ" + infrastructure this step must never sweep into
# the archive even though it sits at DATA_HOME's top level: `worktrees/`
# (real git checkouts `worktree_manager.py` owns — `LEGACY_MAPPING`'s own
# note: "never copied"), and the archive destination itself.
_ARCHIVE_SKIP_NAMES: frozenset[str] = (
    frozenset(
        {
            "claude-config",
            "codex-home",
            "opencode-home",
            "worktrees",
            "venv",
            "skills",
            "psmodules",
            _ARCHIVE_DIR_NAME,
            _LEGACY_V2_NAME,
        }
    )
    | _V2_TOP_LEVEL_NAMES
)

# #504 item 5 — explicitly named as "not data", deleted outright rather than
# archived. Exact names only; nothing here is a guess.
_DELETE_OUTRIGHT_NAMES: frozenset[str] = frozenset({"openviking", "claude-config.partial"})
_DELETE_OUTRIGHT_GLOB = ".takkub_issues.synced-*.bak.json"


def _undo_moved_entries(entries: list[tuple[Path, Path]]) -> None:
    """Best-effort reversal of a copy-verified-then-remove-source phase that
    may have gotten only partway through removing sources — *entries* is
    (original_src, copied_dest) pairs collected as the copy phase ran, in
    the SAME call before the (separate) removal loop starts. If *src* still
    exists, removal never reached it: just drop the half-made *dest* copy.
    If *src* is already gone, removal succeeded for that entry: copy *dest*
    back to *src* before dropping *dest*, so every entry ends up exactly
    where it started, regardless of exactly where the removal loop failed."""
    for src, dest in entries:
        try:
            if not src.exists() and dest.exists():
                if dest.is_dir():
                    shutil.copytree(dest, src)
                else:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, src)
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            elif dest.exists():
                dest.unlink()
        except OSError:
            pass


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


# ---------------------------------------------------------------------------
# Step — promote a pre-existing nested v2/ root up to the top level
# ---------------------------------------------------------------------------


@dataclass
class PromoteV2RootStep:
    step_id: str = "promote-v2-root"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)

    def _legacy_root(self) -> Path:
        return self.data_home / _LEGACY_V2_NAME

    def _manifest_path(self) -> Path:
        # A fixed name, NOT nested inside `ArchiveV1LegacyStep`'s own
        # timestamped archive dir — this step runs BEFORE that step picks
        # its timestamp, so it needs a location of its own to record which
        # top-level names it moved, for its own `rollback()`/`restore-v1`
        # to read back later. Overwritten on every successful promote —
        # there is only ever one "currently promoted" state to describe.
        return self.data_home / _ARCHIVE_DIR_NAME / "promote-v2-root-manifest.json"

    def _pending(self) -> bool:
        # An empty `v2/` shell doesn't count — this step's own job is only
        # to move its CONTENTS up; the (by then empty) folder itself is
        # `ArchiveV1LegacyStep`'s to remove. Treating a bare empty dir as
        # still-pending would make a re-run's `_promote_candidates()` come
        # back empty and overwrite this step's own manifest with an empty
        # one, losing the record `rollback()`/`restore-v1` needs.
        root = self._legacy_root()
        return root.is_dir() and any(root.iterdir())

    def _promote_candidates(self) -> list[Path]:
        root = self._legacy_root()
        if not root.is_dir():
            return []
        try:
            return sorted(root.iterdir())
        except OSError:
            return []

    def inspect(self) -> StepReport:
        pending = self._pending()
        names = [p.name for p in self._promote_candidates()]
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"legacy v2/ root {'present' if pending else 'absent'}; "
            f"{len(names)} item(s) would be promoted to {self.data_home}",
            detail={"pending": pending, "items": names},
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will move every entry under {self._legacy_root()} up into {self.data_home} "
            "(merged into any same-named entry already there)"
            if self._pending()
            else "no legacy v2/ root — nothing to promote",
        )

    def dry_run(self) -> StepReport:
        names = [p.name for p in self._promote_candidates()]
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; {len(names)} item(s) would be promoted"
            if names
            else "nothing pending",
            detail={"items": names},
        )

    def apply(self) -> StepReport:
        if not self._pending():
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id, "apply", True, "no legacy v2/ root — nothing to promote"
            )

        done: list[tuple[Path, Path]] = []
        try:
            candidates = self._promote_candidates()
            for src in candidates:
                dest = self.data_home / src.name
                copy_verified(src, dest)
                done.append((src, dest))
            for src, _dest in done:
                _remove(src)
            write_json_atomic(
                self._manifest_path(),
                {
                    "schema": 1,
                    "created_at": time.time(),
                    "promoted": [{"name": s.name} for s, _d in done],
                },
            )
        except (OSError, VerifyMismatchError) as e:
            _undo_moved_entries(done)
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"promote failed, rolled back: {e}")

        self.journal.record(self.step_id, "apply", True, f"promoted {len(done)} item(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"promoted {len(done)} item(s) from {self._legacy_root()} to {self.data_home}",
            detail={"items": [s.name for s, _ in done]},
        )

    def validate(self) -> StepReport:
        pending = self._pending()
        ok = not pending
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "promoted — no legacy v2/ root remains" if ok else "legacy v2/ root still present",
        )

    def rollback(self) -> StepReport:
        """Re-create ``v2/`` and move the currently-top-level content this
        step promoted back into it — the pair to `ArchiveV1LegacyStep
        .rollback()`, and what `takkub migrate restore-v1` calls alongside
        it. A no-op when this step never actually promoted anything."""
        manifest_path = self._manifest_path()
        if not manifest_path.is_file():
            self.journal.record(self.step_id, "rollback", True, "never promoted — nothing to undo")
            return StepReport(
                self.step_id, "rollback", True, "no promote manifest — nothing to undo"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"could not read manifest: {e}")

        legacy_root = self._legacy_root()
        restored: list[str] = []
        try:
            for entry in manifest.get("promoted", []):
                name = entry["name"]
                src = self.data_home / name
                dest = legacy_root / name
                if not src.exists():
                    continue
                copy_verified(src, dest)
                _remove(src)
                restored.append(name)
        except (OSError, VerifyMismatchError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")

        self.journal.record(self.step_id, "rollback", True, f"restored {len(restored)} item(s)")
        return StepReport(
            self.step_id,
            "rollback",
            True,
            f"moved {len(restored)} item(s) back under {legacy_root}",
            detail={"restored": restored},
        )


# ---------------------------------------------------------------------------
# Step — archive whatever V1 top-level leftovers remain, delete #504 item 5's
# "ศพเก่า", and archive the now-empty legacy v2/ folder too
# ---------------------------------------------------------------------------


@dataclass
class ArchiveV1LegacyStep:
    step_id: str = "archive-v1-legacy"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)

    _run_id: ClassVar[str] = ""  # unused; timestamp is computed per apply()

    def _legacy_root(self) -> Path:
        return self.data_home / _LEGACY_V2_NAME

    def _archive_base(self) -> Path:
        return self.data_home / _ARCHIVE_DIR_NAME

    def _pending(self) -> bool:
        """There is work for this step whenever either a genuine V1
        top-level leftover, a #504-item-5 junk entry, or the (already
        emptied-by-`PromoteV2RootStep`) legacy v2/ folder is still on disk —
        this step is idempotent/no-op once all three are gone."""
        return (
            bool(self._archive_candidates())
            or bool(self._delete_candidates())
            or self._legacy_root().is_dir()
        )

    def _delete_candidates(self) -> list[Path]:
        if not self.data_home.is_dir():
            return []
        out = []
        for p in sorted(self.data_home.iterdir()):
            if p.name in _DELETE_OUTRIGHT_NAMES or fnmatch.fnmatch(p.name, _DELETE_OUTRIGHT_GLOB):
                out.append(p)
        return out

    def _archive_candidates(self) -> list[Path]:
        if not self.data_home.is_dir():
            return []
        delete_names = {p.name for p in self._delete_candidates()}
        return [
            p
            for p in sorted(self.data_home.iterdir())
            if p.name not in _ARCHIVE_SKIP_NAMES and p.name not in delete_names
        ]

    def inspect(self) -> StepReport:
        archive_n = self._archive_candidates()
        delete_n = self._delete_candidates()
        legacy_empty = self._legacy_root().is_dir()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(archive_n)} V1 top-level item(s) to archive, {len(delete_n)} junk item(s) "
            f"to delete outright, legacy v2/ {'present' if legacy_empty else 'absent'}",
            detail={
                "archive_candidates": [p.name for p in archive_n],
                "delete_candidates": [p.name for p in delete_n],
            },
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will move V1 leftovers into {self._archive_base()}/v1-archive-<ts>/, "
            "delete #504-item-5 junk outright, archive the emptied v2/ folder too",
        )

    def dry_run(self) -> StepReport:
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            "no disk writes; "
            f"{len(self._archive_candidates())} item(s) would be archived, "
            f"{len(self._delete_candidates())} junk item(s) would be deleted",
        )

    def apply(self) -> StepReport:
        if not self._pending():
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id, "apply", True, "no V1 leftovers found — nothing to archive"
            )

        ts = f"{time.time():.6f}"
        archive_root = self._archive_base() / f"v1-archive-{ts}"
        done_archive: list[tuple[Path, Path]] = []
        try:
            for src in self._archive_candidates():
                dest = archive_root / src.name
                copy_verified(src, dest)
                done_archive.append((src, dest))
            for src, _dest in done_archive:
                _remove(src)

            legacy_root = self._legacy_root()
            archived_legacy_marker = False
            if legacy_root.is_dir():
                (archive_root / _LEGACY_V2_NAME).mkdir(parents=True, exist_ok=True)
                shutil.rmtree(legacy_root)
                archived_legacy_marker = True

            manifest = {
                "schema": 1,
                "created_at": time.time(),
                "archived": [{"name": s.name, "path": str(d)} for s, d in done_archive],
                "legacy_v2_marker_archived": archived_legacy_marker,
                "deleted": [],
            }
            write_json_atomic(archive_root / _MANIFEST_NAME, manifest)
        except (OSError, VerifyMismatchError) as e:
            _undo_moved_entries(done_archive)
            for d in (archive_root, self._archive_base()):
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"archive failed, rolled back: {e}")

        # #504 item 5: delete outright, only AFTER the archive above has
        # fully succeeded and verified — a failure here just leaves a bit of
        # named junk clutter (never data), so it doesn't unwind the archive.
        delete_failures: list[str] = []
        deleted: list[str] = []
        for p in self._delete_candidates():
            try:
                _remove(p)
                deleted.append(p.name)
            except OSError as e:
                delete_failures.append(f"{p.name}: {e}")
        if deleted or delete_failures:
            try:
                manifest_path = archive_root / _MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["deleted"] = deleted
                if delete_failures:
                    manifest["delete_failures"] = delete_failures
                write_json_atomic(manifest_path, manifest)
            except OSError:
                pass

        self.journal.record(
            self.step_id,
            "apply",
            True,
            f"archived {len(done_archive)} item(s), deleted {len(deleted)} junk item(s)",
        )
        summary = f"archived {len(done_archive)} V1 item(s) into {archive_root}, deleted {len(deleted)} junk item(s)"
        if delete_failures:
            summary += (
                f" ({len(delete_failures)} junk item(s) could not be deleted — harmless clutter)"
            )
        return StepReport(
            self.step_id,
            "apply",
            True,
            summary,
            detail={"archive_root": str(archive_root), "delete_failures": delete_failures},
        )

    def validate(self) -> StepReport:
        pending = self._pending()
        ok = not pending
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "no V1 leftovers remain"
            if ok
            else "V1 leftover(s) still present — archive not yet applied",
        )

    def rollback(self) -> StepReport:
        """`takkub migrate restore-v1`'s core: COPY every archived item back
        to its original top-level spot (never move — the archive itself is
        never deleted or expired by this codebase, #504 item 2) from the
        most recent archive manifest. #504 item 5's deleted junk is NOT
        recoverable by design — that's the whole point of deleting it
        outright instead of archiving it."""
        manifest_path = _find_latest_manifest(self.data_home)
        if manifest_path is None:
            self.journal.record(self.step_id, "rollback", True, "no archive to restore from")
            return StepReport(
                self.step_id, "rollback", True, "no v1-archive found — nothing to restore"
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"could not read manifest: {e}")

        archive_root = manifest_path.parent
        restored: list[str] = []
        try:
            for entry in manifest.get("archived", []):
                name = entry["name"]
                src = Path(entry["path"])
                dest = self.data_home / name
                if not src.exists():
                    continue
                copy_verified(src, dest)
                restored.append(name)
        except (OSError, VerifyMismatchError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")

        self.journal.record(self.step_id, "rollback", True, f"restored {len(restored)} item(s)")
        deleted = manifest.get("deleted") or []
        note = (
            f" — {len(deleted)} #504-item-5 junk item(s) were deleted, not recoverable"
            if deleted
            else ""
        )
        return StepReport(
            self.step_id,
            "rollback",
            True,
            f"restored {len(restored)} item(s) from {archive_root}{note}",
            detail={"archive_root": str(archive_root), "restored": restored},
        )


def _find_latest_manifest(data_home: Path) -> Path | None:
    base = data_home / _ARCHIVE_DIR_NAME
    if not base.is_dir():
        return None
    try:
        candidates = sorted(
            (p for p in base.iterdir() if p.is_dir() and p.name.startswith("v1-archive-")),
            reverse=True,
        )
    except OSError:
        return None
    for c in candidates:
        m = c / _MANIFEST_NAME
        if m.is_file():
            return m
    return None
