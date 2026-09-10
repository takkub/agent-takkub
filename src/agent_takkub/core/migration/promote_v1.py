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
ย้าย"). A failure while copy-verifying (Phase A, `_two_phase_move` — no
*src* has been touched yet) undoes every `dest` this call just created; a
failure while removing an already-verified source (Phase B) instead
restores every *src* Phase B already removed — it NEVER deletes a `dest`,
because an earlier pair's `dest` may by then be the only remaining copy of
its data (#504 R2-B1, below). So a partial move never lingers (#504: "ล้ม
กลางทาง → journal rollback กลับสภาพเดิมทั้งก้อน ห้ามค้างครึ่ง") — on top of
that, `rollback()`/the CLI's `takkub migrate restore-v1` can always
reconstruct the pre-2.1.0 shape later from the archive's own manifest,
since that archive is never deleted or expired by this codebase.

2026-09-10 acceptance review (#504) findings B2/B3/B4/B5, H1/H2/H3/H6/H7/H9
(docs/audit/2026-09-10-504-acceptance-review.md) rewrote most of the actual
move/undo/restore machinery below — every mutating operation now goes
through `_two_phase_move`/`_copy_only_transaction` + `BackupManager`-backed
preimages, manifests record per-file relative paths (never a bare top-level
name) with sha256 digests, and archives store archive-relative paths so a
relocated `backups/` tree still restores. See each helper's own docstring
for the specific failure mode it closes.

Round 2 of that same review (`## Round 2 — 60abb771` in the same doc) found
the Phase A/Phase B conflation above (R2-B1: a caught Phase B failure still
triggered the Phase A-style `dest`-deleting undo, dropping an EARLIER
pair's already-source-removed data to zero copies), restore-v1 accepting a
missing/corrupt archived member or an unknown `--archive` selector (R2-H1),
a registered provider home nested more than one level deep still being
archived (R2-H2), an incomplete disk preflight (R2-H3, `auto_migrate_boot
.py`), `MigrationEngine.validate()`/`ArchiveV1LegacyStep.validate()` still
not checking real target/older-generation integrity (#568's H9 gap), and no
durable per-item progress before a source removal (the crash/restart gap).
Every one of those is closed in this module (`_two_phase_move`'s
`on_before_remove`/`on_restore` callbacks, `_archive_entry_problems` reused
fail-closed by `rollback()`, `_named_account_home_names`'s first-path-
segment protection, `_find_all_manifests`) or its caller
(`engine.py`'s `_domain_target_problems`, `auto_migrate_boot
._estimate_copy_bytes`, `cli._cmd_migrate_restore_v1`'s multi-generation
undo).
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_takkub import config

from .backup import BackupManager
from .journal import MigrationJournal
from .registry_copy_step import write_json_atomic
from .report import StepReport
from .verify_copy import VerifyMismatchError, _sha256, copy_verified

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
# directory name with a V2 one. #504 H7: a NAMED V1 file living one level
# INSIDE one of these shared directories (`agents/<role>.md` next to V2's own
# `agents/custom/`; `projects/<slug>/role-providers.json` next to V2's own
# `projects/<id>/project.json`) is a real, still-live V1 leftover though —
# `_SHARED_DIR_LEGACY_GLOBS` below archives exactly those files, one at a
# time, without ever sweeping the shared directory itself.
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
#
# #504 B4 (acceptance review): a static exact-name set can only ever protect
# the DEFAULT provider homes (`claude-config`, `codex-home`,
# `opencode-home`) — a *named* account's home
# (`accounts_adapter.default_account_home`, e.g. `claude-config-team`) has
# no fixed basename this set could enumerate, and the registry that lists
# them (`user-profiles.json`) is itself still a live read/write target of
# `user_profile.py`, not a retired V1 source. `core-v2-settings.json`
# (`core_v2_settings.path()`) and `project-skills` (`config
# .PROJECT_SKILLS_HOME`) are two more still-active top-level files/dirs the
# original list missed entirely. `_named_account_home_names()` below
# additionally protects whatever `user-profiles.json` ACTUALLY points at,
# rather than guessing a naming convention.
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
            "user-profiles.json",
            "core-v2-settings.json",
            "project-skills",
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

# #504 H7: known V1-only files that live ONE LEVEL INSIDE a shared V2
# top-level directory (`_V2_TOP_LEVEL_NAMES` skips the whole directory to
# protect V2's own content there). Each glob is relative to `data_home` and
# matched non-recursively against real V1 sources named in
# `core.storage.layout.LEGACY_MAPPING` (`CUSTOM_AGENTS_DIR/<role>.md`,
# `SETTINGS_HOME/projects/<slug>/role-providers.json`) — `steps_v1.py`'s
# `RoleAgentMigrationStep`/`ProjectMigrationStep` already folded each file's
# CONTENT into its V2 target (`agents/custom/registry.json` /
# `config/routing.json`); these are the original V1 files left behind
# afterward, so they're archived (never deleted outright) same as every
# other real V1 leftover.
_SHARED_DIR_LEGACY_GLOBS: tuple[str, ...] = (
    "agents/*.md",
    "projects/*/role-providers.json",
)


def _rel_files(root: Path) -> list[Path]:
    if root.is_file():
        return [Path(".")]
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


DoneEntry = tuple[Path, Path, "Path | None", dict[str, str]]
# (src, dest, error message) — a pair whose *src* could not be removed in
# Phase B (before restoration below put it back).
CleanupPendingEntry = tuple[Path, Path, str]


@dataclass(frozen=True, slots=True)
class MoveResult:
    """Outcome of `_two_phase_move`, split by WHICH phase it failed in —
    #504 acceptance-review round 2 R2-B1 (was B-new): Phase A (copy+verify)
    and Phase B (remove sources) fail in fundamentally different ways, and
    treating them the same used to be able to drop a file to ZERO copies.

    A `phase_a_error` means no *src* was ever touched — every `done` entry
    is a *dest* this call just created and nothing else, so the caller's
    `_undo_moved_entries` can safely delete every one of them; the original
    data is still sitting at its *src*, untouched.

    A non-empty `cleanup_pending` means Phase B is where things went wrong:
    at least one pair's *src* removal raised, possibly AFTER an earlier
    pair's *src* was already removed in this same phase. Deleting `dest`
    here (an "undo") is never safe — that earlier pair's `dest` may be the
    ONLY remaining copy of its data. Restoring the just-deleted `src` back
    from its already-verified `dest` is: `_two_phase_move` does exactly
    that for every pair whose *src* it removed before returning, so by the
    time this result comes back EVERY pair's *src* is present again — the
    whole transaction reverts to "every dest verified, no source removed",
    safely re-driveable by a plain retry. `cleanup_pending` records which
    pair(s) actually failed, for the caller to log/report; `done`'s *dest*
    entries are untouched either way."""

    done: list[DoneEntry]
    phase_a_error: Exception | None = None
    cleanup_pending: list[CleanupPendingEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.phase_a_error is None and not self.cleanup_pending


def _restore_removed_source(src: Path, dest: Path) -> None:
    """Re-materialize *src* by copying the already copy-verified *dest*
    back onto it — never touches *dest*. Best-effort: even if this itself
    fails, the invariant "at least one copy survives" still holds (*dest*
    is untouched throughout), so a failure here is not escalated further."""
    try:
        if dest.is_dir():
            shutil.copytree(dest, src, dirs_exist_ok=True)
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, src)
    except OSError:
        pass


def _two_phase_move(
    pairs: list[tuple[Path, Path]],
    backups: BackupManager,
    step_id: str,
    *,
    on_before_remove: Callable[[Path, Path, dict[str, str]], None] | None = None,
    on_restore: Callable[[Path, Path], None] | None = None,
) -> MoveResult:
    """Phase A: copy-verify every ``(src, dest)`` pair, back up any
    PRE-EXISTING *dest* first (so a merge into an already-populated
    directory — the kimi-credentials scenario, #504 B3 — can be undone
    byte-for-byte instead of guessed at). No *src* is touched during this
    phase, so a Phase A failure can always be undone in full — see
    `MoveResult`.

    Phase B (only reached once every pair in Phase A copy-verified clean):
    remove every *src*, one at a time. If ANY removal raises, EVERY *src*
    already removed in this same phase is restored back from its
    already-verified *dest* before returning (`_restore_removed_source`) —
    never an undo of `dest` (#504 R2-B1, was B-new: this exact
    replace-then-undo-the-wrong-side sequence used to be reproducible via
    `promote`, `archive`, and `promote`'s own `rollback`, all of which
    share this helper). The caller sees every pair's *src* present again
    and `cleanup_pending` naming which one(s) actually failed.

    ``on_before_remove`` (#504 R2 `crash_restart_restore`), when given, is
    called with ``(src, dest, digests)`` for each pair IMMEDIATELY BEFORE
    its *src* is removed — a caller durably records "this pair is now the
    caller's responsibility to track" (e.g. merges it into its own
    manifest) at the one moment that matters: a process death during or
    right after the actual OS-level removal (which this cannot catch — a
    `KeyboardInterrupt`/`SystemExit`/hard kill is not an `OSError`) still
    leaves that record in place, because it was written BEFORE the
    destructive call, not after. A callback failure is swallowed — it must
    never block the underlying removal it is only asked to precede.

    ``on_restore`` is called with ``(src, dest)`` for each pair
    `_restore_removed_source` puts back after a Phase B failure — the
    compensating retraction for whatever `on_before_remove` durably
    recorded for that same pair, so an ORDINARY caught failure (as opposed
    to the process death `on_before_remove` exists for) leaves no stale
    record behind for a pair whose source turned out to still be present."""
    done: list[DoneEntry] = []
    try:
        for src, dest in pairs:
            backup_path = backups.backup(step_id, dest) if dest.exists() else None
            digests: dict[str, str] = {}
            try:
                digests = copy_verified(src, dest).digests
            finally:
                # #504 B3 `partial_copy`: append even on a raise, so a copy
                # that wrote a partial *dest* and then raised still lands in
                # `done` for `_undo_moved_entries` to clean up.
                done.append((src, dest, backup_path, digests))
    except (OSError, VerifyMismatchError) as e:
        return MoveResult(done=done, phase_a_error=e)

    cleanup_pending: list[CleanupPendingEntry] = []
    removed: list[tuple[Path, Path]] = []
    for src, dest, _backup, digests in done:
        if on_before_remove is not None:
            try:
                on_before_remove(src, dest, digests)
            except Exception:
                pass
        try:
            _remove(src)
            removed.append((src, dest))
        except OSError as e:
            cleanup_pending.append((src, dest, str(e)))

    if cleanup_pending:
        for src, dest in removed:
            _restore_removed_source(src, dest)
            if on_restore is not None:
                try:
                    on_restore(src, dest)
                except Exception:
                    pass
    return MoveResult(done=done, cleanup_pending=cleanup_pending)


def _format_cleanup_pending(cleanup_pending: list[CleanupPendingEntry]) -> str:
    return "; ".join(f"{src}: {msg}" for src, _dest, msg in cleanup_pending)


def _copy_only_transaction(
    pairs: list[tuple[Path, Path]], backups: BackupManager, step_id: str
) -> tuple[list[DoneEntry], Exception | None]:
    """Same verified-copy-with-preimage contract as `_two_phase_move`, but
    *src* is NEVER removed — the `restore-v1` contract (#504 item 2): an
    archive is copied back to its original spot, and stays intact forever
    so a retry or a different `--archive` generation can always reach it
    again. Also backs up *dest* first (#504 H3 `restore_collision` finding:
    restoring into a top-level name that was already recreated since the
    archive used to just overwrite it, discarding the current content with
    no way to get it back)."""
    done: list[DoneEntry] = []
    try:
        for src, dest in pairs:
            backup_path = backups.backup(step_id, dest) if dest.exists() else None
            digests: dict[str, str] = {}
            try:
                digests = copy_verified(src, dest).digests
            finally:
                done.append((src, dest, backup_path, digests))
        return done, None
    except (OSError, VerifyMismatchError) as e:
        return done, e


def _undo_moved_entries(entries: list[DoneEntry]) -> None:
    """Reverse every ``(src, dest, backup_path)`` triple `_two_phase_move`/
    `_copy_only_transaction` attempted: *dest* is removed, then restored
    from *backup_path* if one was taken (a pre-existing merge target) —
    never a bare ``shutil.rmtree`` of a directory that held content before
    this transaction ever started (#504 B3: the old undo deleted the WHOLE
    destination, including whatever pre-dated the merge). *src* is never
    touched — both transaction helpers only remove *src* AFTER full success,
    so an in-progress *src* is always still exactly where it started."""
    for _src, dest, backup_path, _digests in entries:
        try:
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            elif dest.exists():
                dest.unlink()
            if backup_path is not None:
                if backup_path.is_dir():
                    shutil.copytree(backup_path, dest)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, dest)
        except OSError:
            pass


def _prune_empty_dirs(base: Path, relpaths: list[str]) -> None:
    """After moving exactly the files named in *relpaths* out of *base*,
    remove whichever of their parent directories are now empty — never
    *base* itself (it may still hold content this transaction never
    touched, e.g. a live provider's OWN subdirectory sitting next to the
    one that got restored)."""
    seen: set[Path] = set()
    for rel in relpaths:
        p = Path(rel).parent
        while str(p) not in (".", ""):
            seen.add(p)
            p = p.parent
    for d in sorted(seen, key=lambda p: len(p.parts), reverse=True):
        target = base / d
        try:
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        except OSError:
            pass


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _rmdir_tree(root: Path) -> None:
    """Remove *root* only if every directory under it is already empty of
    files — plain `os.rmdir` per directory (never `shutil.rmtree`), so a
    surprise file anywhere in the tree raises `OSError` instead of being
    silently deleted (#504 B2: `ArchiveV1LegacyStep` used to
    unconditionally `shutil.rmtree` the legacy `v2/` root the instant it
    merely EXISTED, with no check that `PromoteV2RootStep` actually
    finished draining it first)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if filenames:
            raise OSError(f"refusing to remove non-empty directory: {dirpath}")
        for d in dirnames:
            os.rmdir(os.path.join(dirpath, d))
    os.rmdir(root)


def list_v1_archives(data_home: Path) -> list[dict]:
    """Every ``v1-archive-<ts>`` generation directory under *data_home*,
    NEWEST first — #504 H1: `restore-v1` used to only ever be able to reach
    the single latest generation, silently stranding files that were only
    ever recorded in an earlier one. `takkub migrate restore-v1 --list`
    surfaces this; `--archive <ts>` then targets one generation explicitly.

    #504 R2-H1: a generation whose manifest.json is missing or unreadable
    is now INCLUDED with ``"unreadable": True`` (and every other field
    ``None``/empty) instead of being silently dropped from the list — an
    operator deciding which `--archive <ts>` to restore from needs to know
    a generation exists but can't be read, not have it vanish as if it
    never happened."""
    base = data_home / _ARCHIVE_DIR_NAME
    if not base.is_dir():
        return []
    try:
        candidates = sorted(
            (p for p in base.iterdir() if p.is_dir() and p.name.startswith("v1-archive-")),
            reverse=True,
        )
    except OSError:
        return []
    out: list[dict] = []
    for c in candidates:
        ts = c.name.removeprefix("v1-archive-")
        m = c / _MANIFEST_NAME
        if not m.is_file():
            out.append(
                {
                    "ts": ts,
                    "path": str(c),
                    "created_at": None,
                    "archived": [],
                    "deleted": [],
                    "unreadable": True,
                    "error": "manifest.json missing",
                }
            )
            continue
        try:
            manifest = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append(
                {
                    "ts": ts,
                    "path": str(c),
                    "created_at": None,
                    "archived": [],
                    "deleted": [],
                    "unreadable": True,
                    "error": str(e),
                }
            )
            continue
        out.append(
            {
                "ts": ts,
                "path": str(c),
                "created_at": manifest.get("created_at"),
                "archived": [e.get("name") for e in manifest.get("archived", [])],
                "deleted": manifest.get("deleted", []),
                "unreadable": False,
            }
        )
    return out


def _find_manifest_by_ts(data_home: Path, ts: str) -> Path | None:
    m = data_home / _ARCHIVE_DIR_NAME / f"v1-archive-{ts}" / _MANIFEST_NAME
    return m if m.is_file() else None


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

    def _merge_manifest_entry(self, entry: dict) -> None:
        """Durably record that *entry* (one candidate's name/kind/paths) is
        promoted — merged into any existing manifest rather than
        overwritten, and called (via `_two_phase_move`'s
        ``on_before_remove``) BEFORE this candidate's `v2/<name>` source is
        removed (#504 R2 `crash_restart_restore`). A process death right
        after that removal — this call already wrote *entry* first — still
        leaves it correctly recorded; a LATER retry's own candidates (which
        no longer include an already-removed entry) merge in beside it
        instead of a batch overwrite silently dropping it, which is what
        used to make `restore-v1` lose the original nested path for
        whatever this run's retry didn't happen to touch."""
        path = self._manifest_path()
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            promoted = list(existing.get("promoted", []))
        except (OSError, ValueError):
            promoted = []
        promoted = [e for e in promoted if e.get("name") != entry["name"]]
        promoted.append(entry)
        write_json_atomic(path, {"schema": 2, "created_at": time.time(), "promoted": promoted})

    def _retract_manifest_entry(self, name: str) -> None:
        """Undo `_merge_manifest_entry` for *name* — called (via
        `_two_phase_move`'s ``on_restore``) when an ORDINARY caught Phase B
        failure puts that candidate's source back, so a ONE-item pending
        state doesn't leave it wrongly recorded as promoted."""
        path = self._manifest_path()
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            promoted = list(existing.get("promoted", []))
        except (OSError, ValueError):
            return
        promoted = [e for e in promoted if e.get("name") != name]
        write_json_atomic(path, {"schema": 2, "created_at": time.time(), "promoted": promoted})

    def apply(self) -> StepReport:
        if not self._pending():
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id, "apply", True, "no legacy v2/ root — nothing to promote"
            )

        candidates = self._promote_candidates()
        # Snapshot each candidate's manifest info BEFORE any mutation —
        # `_two_phase_move` removes `src` on success, so this is the only
        # safe moment to read what it actually contains (#504 B3: the old
        # manifest recorded only the bare top-level name, so `rollback()`
        # could only ever move the WHOLE current top-level dir back,
        # sweeping up anything else that happened to share its name — e.g.
        # a live Kimi credential directory living beside a promoted
        # `providers/claude/`).
        manifest_entries = []
        for src in candidates:
            if src.is_dir():
                paths = [p.as_posix() for p in _rel_files(src)]
                manifest_entries.append({"name": src.name, "kind": "dir", "paths": paths})
            else:
                manifest_entries.append({"name": src.name, "kind": "file"})
        entries_by_name = {e["name"]: e for e in manifest_entries}

        pairs = [(src, self.data_home / src.name) for src in candidates]
        result = _two_phase_move(
            pairs,
            self.backups,
            self.step_id,
            on_before_remove=lambda src, _dest, _digests: self._merge_manifest_entry(
                entries_by_name[src.name]
            ),
            on_restore=lambda src, _dest: self._retract_manifest_entry(src.name),
        )
        if result.phase_a_error is not None:
            _undo_moved_entries(result.done)
            self.journal.record(self.step_id, "apply", False, str(result.phase_a_error))
            return StepReport(
                self.step_id, "apply", False, f"promote failed, rolled back: {result.phase_a_error}"
            )

        if result.cleanup_pending:
            # #504 R2-B1: at least one candidate's `v2/<name>` source could
            # not be removed after its copy already verified clean.
            # `_two_phase_move` has already restored every source it DID
            # remove in this same phase, so nothing here actually promoted
            # — no manifest write, no legacy-root cleanup. The next
            # `apply()` retry sees the exact same candidates again.
            msg = _format_cleanup_pending(result.cleanup_pending)
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {msg}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                f"promote copy-verified {len(result.done)} item(s) but {len(result.cleanup_pending)} "
                f"source(s) could not be removed — every source has been restored, nothing lost, "
                f"retry apply: {msg}",
                detail={"cleanup_pending": [src.name for src, _d, _m in result.cleanup_pending]},
            )

        # No batch manifest write here — every entry in `manifest_entries`
        # was already durably merged in one at a time via
        # `on_before_remove`, BEFORE its own source removal (#504 R2
        # `crash_restart_restore`). A bulk overwrite using only THIS run's
        # `manifest_entries` would silently drop any entry a PRIOR
        # interrupted run already merged in but that isn't a candidate this
        # time (its `v2/<name>` source is already gone) — exactly the bug
        # this incremental-merge design exists to close.
        self.journal.record(self.step_id, "apply", True, f"promoted {len(result.done)} item(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"promoted {len(result.done)} item(s) from {self._legacy_root()} to {self.data_home}",
            detail={"items": [src.name for src, _d, _b, _dg in result.done]},
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
        it. A no-op when this step never actually promoted anything.

        #504 B3/H3: restores EXACTLY the relative files the manifest
        recorded as promoted, never the whole current top-level directory —
        a live provider home (or anything else) sharing that top-level name
        is never touched, even if it sits right beside what gets restored.
        All-or-nothing per call: a mid-restore failure reverses every entry
        this call already moved back, via the same preimage-backed undo
        `apply()` itself uses (previously this loop could return after
        partially moving entries with no reversal — #504 H3)."""
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
        pairs: list[tuple[Path, Path]] = []
        prune_map: dict[Path, list[str]] = {}
        for entry in manifest.get("promoted", []):
            name = entry["name"]
            kind = entry.get("kind", "file")
            if kind == "file":
                src = self.data_home / name
                if src.exists():
                    pairs.append((src, legacy_root / name))
                continue
            paths = entry.get("paths")
            if paths is None:
                # Pre-#504-fix manifest (schema 1) — no per-file record
                # exists to restore surgically. Skip rather than guess at
                # moving the whole (possibly-merged) top-level dir back.
                continue
            base = self.data_home / name
            prune_map[base] = paths
            for rel in paths:
                s = base / rel
                if s.is_file():
                    pairs.append((s, legacy_root / name / rel))

        result = _two_phase_move(pairs, self.backups, self.step_id)
        if result.phase_a_error is not None:
            _undo_moved_entries(result.done)
            self.journal.record(self.step_id, "rollback", False, str(result.phase_a_error))
            return StepReport(
                self.step_id, "rollback", False, f"restore failed: {result.phase_a_error}"
            )

        if result.cleanup_pending:
            # #504 R2-B1: same rule as `apply()` — `_two_phase_move` has
            # already restored every top-level source it removed in this
            # same phase, so nothing here actually moved back under
            # `legacy_root` — no pruning, next `rollback()` retry sees the
            # same manifest entries again.
            msg = _format_cleanup_pending(result.cleanup_pending)
            self.journal.record(self.step_id, "rollback", False, f"cleanup-pending: {msg}")
            return StepReport(
                self.step_id,
                "rollback",
                False,
                f"restore copy-verified {len(result.done)} item(s) but {len(result.cleanup_pending)} "
                f"could not be removed from their original location — every source has been "
                f"restored, nothing lost, retry rollback: {msg}",
                detail={"cleanup_pending": [str(dest) for _s, dest, _m in result.cleanup_pending]},
            )

        for base, paths in prune_map.items():
            _prune_empty_dirs(base, paths)

        restored = [str(dest.relative_to(legacy_root)) for _s, dest, _b, _dg in result.done]
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
        top-level leftover, a #504-item-5 junk entry, a shared-directory V1
        file (#504 H7), or the (already emptied-by-`PromoteV2RootStep`)
        legacy v2/ folder is still on disk — this step is idempotent/no-op
        once all of those are gone."""
        return (
            bool(self._archive_candidates())
            or bool(self._delete_candidates())
            or bool(self._shared_dir_legacy_candidates())
            or self._legacy_root().is_dir()
        )

    def _named_account_home_names(self) -> set[str]:
        """Every top-level basename under `data_home` that a LIVE
        `user-profiles.json` entry's `config_dir` actually lives under (#504
        B4, + B4-residual). A named account's home shares no fixed basename
        a static skip-list could enumerate
        (`accounts_adapter.default_account_home`: `claude-config-<name>`),
        so this reads the real registry instead of guessing a naming
        convention. Best-effort: a missing/corrupt registry just means
        nothing extra gets protected here, same as every other best-effort
        read in this module.

        B4-residual: `config_dir` need not sit directly at `data_home`'s top
        level — a registered home can be nested arbitrarily deep (e.g.
        `data_home/homes/team/claude-config-team`). Protecting only an exact
        `p.parent == data_home` match left every OTHER top-level ancestor of
        a nested home unprotected — `_archive_candidates()` only ever
        filters by top-level basename, so it would sweep the WHOLE ancestor
        (`homes/`) into the archive, nested live home included. This adds
        the FIRST path segment under `data_home` for every `config_dir`
        actually rooted there, protecting the whole top-level candidate the
        same coarse-grained way every other entry in this module already
        is — never just the leaf name a shallower match would compute."""
        path = self.data_home / "user-profiles.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(raw, list):
            return set()
        out: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            config_dir = entry.get("config_dir")
            if not config_dir:
                continue
            try:
                rel = Path(config_dir).relative_to(self.data_home)
            except (OSError, ValueError, TypeError):
                continue
            if rel.parts:
                out.add(rel.parts[0])
        return out

    def _merge_archive_manifest_entry(self, archive_root: Path, entry: dict) -> None:
        """Progressively accumulate *entry* into *archive_root*'s own
        manifest.json — called (via `_two_phase_move`'s
        ``on_before_remove``) BEFORE this entry's data_home source is
        removed (#504 R2 crash-durability, same rationale as
        `PromoteV2RootStep._merge_manifest_entry`). Unlike promote's single
        persistent manifest path, `archive_root` is fresh per `apply()`
        call, so this only needs to accumulate WITHIN this one call — but
        without it, a process death mid-loop leaves `archive_root` holding
        real archived files with no manifest.json at all, invisible to
        `list_v1_archives()`/`restore-v1` even though the bytes are safely
        on disk."""
        path = archive_root / _MANIFEST_NAME
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {
                "schema": 2,
                "created_at": time.time(),
                "archived": [],
                "legacy_v2_marker_archived": False,
                "deleted": [],
            }
        archived = [e for e in existing.get("archived", []) if e.get("name") != entry["name"]]
        archived.append(entry)
        existing["archived"] = archived
        write_json_atomic(path, existing)

    def _retract_archive_manifest_entry(self, archive_root: Path, name: str) -> None:
        """Undo `_merge_archive_manifest_entry` for *name* — called (via
        `_two_phase_move`'s ``on_restore``) when an ordinary caught Phase B
        failure puts that entry's source back."""
        path = archive_root / _MANIFEST_NAME
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        existing["archived"] = [e for e in existing.get("archived", []) if e.get("name") != name]
        write_json_atomic(path, existing)

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
        protected = _ARCHIVE_SKIP_NAMES | self._named_account_home_names()
        return [
            p
            for p in sorted(self.data_home.iterdir())
            if p.name not in protected and p.name not in delete_names
        ]

    def _shared_dir_legacy_candidates(self) -> list[Path]:
        if not self.data_home.is_dir():
            return []
        out: list[Path] = []
        for pattern in _SHARED_DIR_LEGACY_GLOBS:
            try:
                out.extend(sorted(p for p in self.data_home.glob(pattern) if p.is_file()))
            except OSError:
                continue
        return out

    def inspect(self) -> StepReport:
        archive_n = self._archive_candidates()
        delete_n = self._delete_candidates()
        shared_n = self._shared_dir_legacy_candidates()
        legacy_empty = self._legacy_root().is_dir()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(archive_n)} V1 top-level item(s) + {len(shared_n)} shared-dir item(s) "
            f"to archive, {len(delete_n)} junk item(s) to delete outright, legacy v2/ "
            f"{'present' if legacy_empty else 'absent'}",
            detail={
                "archive_candidates": [p.name for p in archive_n],
                "shared_dir_candidates": [str(p.relative_to(self.data_home)) for p in shared_n],
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
            f"{len(self._archive_candidates()) + len(self._shared_dir_legacy_candidates())} "
            f"item(s) would be archived, {len(self._delete_candidates())} junk item(s) "
            "would be deleted",
        )

    def apply(self) -> StepReport:
        if not self._pending():
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id, "apply", True, "no V1 leftovers found — nothing to archive"
            )

        legacy_root = self._legacy_root()
        if legacy_root.is_dir():
            leftover = next((p for p in legacy_root.rglob("*") if p.is_file()), None)
            if leftover is not None:
                # #504 B2: the old code only checked `legacy_root.is_dir()`
                # and then unconditionally `shutil.rmtree`d it — a
                # `promote-v2-root` that failed (or was interrupted) mid-copy
                # can leave real un-promoted data here, and deleting it
                # outright would violate #504 rule #1 ("ย้าย ไม่ลบ") outright.
                # Refuse the whole apply instead — the caller's normal
                # failure-handling path (`MigrationEngine.apply_pending()`'s
                # hard stop on a `promote-v2-root` failure) is what should
                # actually prevent this step from ever being reached in that
                # state; this is the belt-and-suspenders backstop for every
                # OTHER caller (`migrate apply`, a hand-built ladder, ...).
                msg = (
                    f"legacy v2/ root at {legacy_root} still holds un-promoted file(s) "
                    f"(e.g. {leftover.relative_to(legacy_root)}) — refusing to delete; "
                    "promote-v2-root must finish (or be rolled back) first"
                )
                self.journal.record(self.step_id, "apply", False, msg)
                return StepReport(self.step_id, "apply", False, msg)

        ts = f"{time.time():.6f}"
        archive_root = self._archive_base() / f"v1-archive-{ts}"
        candidates = self._archive_candidates() + self._shared_dir_legacy_candidates()
        pairs = [(src, archive_root / src.relative_to(self.data_home)) for src in candidates]
        result = _two_phase_move(
            pairs,
            self.backups,
            self.step_id,
            on_before_remove=lambda src, dest, digests: self._merge_archive_manifest_entry(
                archive_root,
                {
                    "name": str(src.relative_to(self.data_home)),
                    "path": dest.relative_to(archive_root).as_posix(),
                    "sha256": digests,
                },
            ),
            on_restore=lambda src, _dest: self._retract_archive_manifest_entry(
                archive_root, str(src.relative_to(self.data_home))
            ),
        )
        if result.phase_a_error is not None:
            _undo_moved_entries(result.done)
            for d in (archive_root, self._archive_base()):
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
            self.journal.record(self.step_id, "apply", False, str(result.phase_a_error))
            return StepReport(
                self.step_id, "apply", False, f"archive failed, rolled back: {result.phase_a_error}"
            )

        if result.cleanup_pending:
            # #504 R2-B1: `_two_phase_move` has already restored every
            # data_home source it removed in this same phase, so nothing
            # here actually archived — no manifest write (an unnamed
            # archive_root with verified-but-unrecorded copies is harmless
            # clutter, cleared out on this generation's next successful
            # retry), no legacy-v2/-folder cleanup, no item-5 deletes. The
            # next `apply()` retry sees the same candidates again.
            msg = _format_cleanup_pending(result.cleanup_pending)
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {msg}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                f"archive copy-verified {len(result.done)} item(s) but {len(result.cleanup_pending)} "
                f"source(s) could not be removed — every source has been restored, nothing lost, "
                f"retry apply: {msg}",
                detail={"cleanup_pending": [str(src) for src, _d, _m in result.cleanup_pending]},
            )

        done = result.done
        archived_legacy_marker = False
        if legacy_root.is_dir():
            (archive_root / _LEGACY_V2_NAME).mkdir(parents=True, exist_ok=True)
            try:
                _rmdir_tree(legacy_root)
                archived_legacy_marker = True
            except OSError as e:
                # #504 B-new: the archive move above already fully
                # succeeded — every src pair's source is gone and every
                # dest verified — so undoing `done` here would delete the
                # ONLY remaining copy of that data over a failure that is
                # scoped to the legacy v2/ marker alone. Record what
                # actually archived and report the marker problem
                # separately instead.
                write_json_atomic(
                    archive_root / _MANIFEST_NAME,
                    {
                        "schema": 2,
                        "created_at": time.time(),
                        "archived": [
                            {
                                "name": str(src.relative_to(self.data_home)),
                                "path": dest.relative_to(archive_root).as_posix(),
                                "sha256": digests,
                            }
                            for src, dest, _backup, digests in done
                        ],
                        "legacy_v2_marker_archived": False,
                        "deleted": [],
                    },
                )
                self.journal.record(self.step_id, "apply", False, str(e))
                return StepReport(
                    self.step_id,
                    "apply",
                    False,
                    f"archived {len(done)} item(s) into {archive_root}, but the legacy v2/ "
                    f"marker could not be removed (no data lost, retry apply to finish): {e}",
                )

        manifest = {
            "schema": 2,
            "created_at": time.time(),
            "archived": [
                {
                    "name": str(src.relative_to(self.data_home)),
                    "path": dest.relative_to(archive_root).as_posix(),
                    "sha256": digests,
                }
                for src, dest, _backup, digests in done
            ],
            "legacy_v2_marker_archived": archived_legacy_marker,
            "deleted": [],
        }
        write_json_atomic(archive_root / _MANIFEST_NAME, manifest)

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
            f"archived {len(done)} item(s), deleted {len(deleted)} junk item(s)",
        )
        summary = f"archived {len(done)} V1 item(s) into {archive_root}, deleted {len(deleted)} junk item(s)"
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
        if pending:
            return StepReport(
                self.step_id,
                "validate",
                False,
                "V1 leftover(s) still present — archive not yet applied",
            )
        # #504 H9 / R2-H9: "nothing left to archive" alone used to be treated
        # as proof the LATEST archive is intact — recompute the sha256 of
        # every file every generation recorded and compare, so silent
        # corruption/deletion of ANY archive generation turns validate() red
        # instead of staying green forever. Checking only the latest left an
        # OLDER generation's corruption invisible even though `restore-v1`
        # (`cli.py`) walks every generation by default, not just the latest.
        manifest_paths = _find_all_manifests(self.data_home)
        if not manifest_paths:
            return StepReport(self.step_id, "validate", True, "no V1 leftovers remain")
        for manifest_path in manifest_paths:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archive manifest unreadable ({manifest_path.parent.name}): {e}",
                )
            archive_root = manifest_path.parent
            problems = _archive_entry_problems(archive_root, manifest)
            if problems:
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archived file {problems[0]} (generation {archive_root.name})",
                )
        return StepReport(self.step_id, "validate", True, "no V1 leftovers remain")

    def rollback(self, archive_ts: str | None = None) -> StepReport:
        """`takkub migrate restore-v1`'s core: COPY every archived item back
        to its original top-level spot (never move — the archive itself is
        never deleted or expired by this codebase, #504 item 2) from ONE
        archive generation's manifest — the latest, unless *archive_ts*
        names a specific ``v1-archive-<ts>`` generation (#504 H1: restore
        used to only ever be able to reach the single latest generation).
        #504 item 5's deleted junk is NOT recoverable by design — that's the
        whole point of deleting it outright instead of archiving it.

        All-or-nothing per call, with each destination's PRE-restore content
        backed up first (#504 H3): a file that already exists at the
        destination is preserved, not silently discarded, and a mid-restore
        failure reverses every entry this call already restored."""
        manifest_path = (
            _find_manifest_by_ts(self.data_home, archive_ts)
            if archive_ts is not None
            else _find_latest_manifest(self.data_home)
        )
        if manifest_path is None:
            if archive_ts is not None:
                # #504 R2-H1: an EXPLICITLY named generation that doesn't
                # exist is a caller error, not "nothing to restore" — the
                # old code returned ok=True here regardless, so
                # `restore-v1 --archive <typo'd-or-stale-ts>` silently did
                # nothing while reporting success.
                msg = f"no v1-archive-{archive_ts} generation found"
                self.journal.record(self.step_id, "rollback", False, msg)
                return StepReport(self.step_id, "rollback", False, msg)
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
        # #504 H2 residual: fail CLOSED — refuse the whole restore, touching
        # nothing at `data_home`, the moment any archived member is missing
        # or corrupt. The old code just skipped a missing `src` per-entry
        # (`if not src.exists(): continue`) and still returned ok=True,
        # silently dropping that file from the restore instead of refusing.
        problems = _archive_entry_problems(archive_root, manifest)
        if problems:
            msg = (
                f"archive at {archive_root} is incomplete/corrupt "
                f"({len(problems)} problem(s)) — refusing to restore: {'; '.join(problems[:5])}"
            )
            self.journal.record(self.step_id, "rollback", False, msg)
            return StepReport(self.step_id, "rollback", False, msg, detail={"problems": problems})

        pairs: list[tuple[Path, Path]] = []
        for entry in manifest.get("archived", []):
            name = entry["name"]
            rel = entry.get("path")
            src = archive_root / rel if rel is not None else Path(entry.get("path", ""))
            pairs.append((src, self.data_home / name))

        done, err = _copy_only_transaction(pairs, self.backups, self.step_id)
        if err is not None:
            _undo_moved_entries(done)
            self.journal.record(self.step_id, "rollback", False, str(err))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {err}")

        restored = [str(dest.relative_to(self.data_home)) for _s, dest, _b, _dg in done]
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

    def _undo_restored_names(self, names: list[str]) -> None:
        """Reverse a PRIOR successful `rollback()` call's restored
        top-level entries — #504 R2-H1: `cli._cmd_migrate_restore_v1` walks
        every generation oldest-first when no `--archive` is given; if a
        LATER generation's restore fails, the EARLIER one(s) this same
        command call already restored must not be left half-applied (a
        multi-generation restore-v1 is all-or-nothing). Each name's
        PRE-restore content — backed up by `_copy_only_transaction` before
        THAT generation's restore overwrote/merged it — is restored back; a
        name with no backup means it didn't exist before the restore, so it
        is simply removed again. Best-effort, same as every other undo path
        in this module: a failure here is swallowed rather than compounding
        the original failure with a second one."""
        for name in names:
            dest = self.data_home / name
            backup_path = self.backups.latest_backup(self.step_id, name)
            try:
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                elif dest.exists():
                    dest.unlink()
                if backup_path is not None:
                    if backup_path.is_dir():
                        shutil.copytree(backup_path, dest)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup_path, dest)
            except OSError:
                pass


def _archive_entry_problems(archive_root: Path, manifest: dict) -> list[str]:
    """Every problem (missing file / checksum mismatch) found while checking
    *manifest*'s ``archived`` entries against what's actually under
    *archive_root* — shared by `ArchiveV1LegacyStep.validate()` (H9, which
    only needs to know the archive is intact) and `.rollback()` (#504 H2
    residual: which must refuse the ENTIRE restore, touching nothing at
    `data_home`, the moment ANY archived member turns out missing or
    corrupt, rather than silently skipping just that one file — via its old
    ``if not src.exists(): continue`` — and reporting success anyway).
    Empty return means the archive is fully intact."""
    problems: list[str] = []
    for entry in manifest.get("archived", []):
        rel = entry.get("path")
        digests: dict[str, str] = entry.get("sha256") or {}
        if not rel or not digests:
            continue
        base = archive_root / rel
        for file_rel, expected in digests.items():
            target = base / file_rel if base.is_dir() else base
            if not target.is_file():
                problems.append(f"missing: {target}")
                continue
            if _sha256(target) != expected:
                problems.append(f"checksum mismatch: {target}")
    return problems


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


def _find_all_manifests(data_home: Path) -> list[Path]:
    """Every ``v1-archive-<ts>`` generation's manifest.json under
    *data_home*, oldest first — #504 R2-H9 `older_archive_integrity`:
    `restore-v1`'s default (`cli.py`) walks EVERY generation, so
    `ArchiveV1LegacyStep.validate()` checking only the latest one left an
    older generation's corruption invisible while still being consumed by
    restore."""
    base = data_home / _ARCHIVE_DIR_NAME
    if not base.is_dir():
        return []
    try:
        candidates = sorted(
            p for p in base.iterdir() if p.is_dir() and p.name.startswith("v1-archive-")
        )
    except OSError:
        return []
    return [c / _MANIFEST_NAME for c in candidates if (c / _MANIFEST_NAME).is_file()]
