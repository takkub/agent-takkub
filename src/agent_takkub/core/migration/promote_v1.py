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
  existing steps run their `validate()` in this same pass.

* `ArchiveV1LegacyStep` (ladder position last, after `core-internal-store`)
  — archive whatever V1 top-level leftovers are STILL on disk into
  ``DATA_HOME/backups/v1-archive-<ts>/`` (never deleted — user directive
  2026-09-07: "ไม่ลบนะ ใช้การย้าย ... เผื่อเอากลับมาจะได้ไม่มีปัญหา"), delete
  the small named set of #504 item 5's "ศพเก่าที่ไม่ใช่ข้อมูล" outright, and
  archive the (by now empty) nested ``v2/`` folder too. Runs LAST precisely
  because every domain step above needs its V1 source still in place to
  read from — archiving first would starve every one of them.

2026-09-10 acceptance review Round 3 (docs/audit/2026-09-10-504-acceptance-
review.md "## Round 3 — 28a4e527", cross-checked against docs/audit/2026-09-
10-504-gemini-crosscheck.md) found that the Round-1/2 fix's shared
copy-then-conditionally-remove helper (`_two_phase_move`) still lost data on
a SECOND failure during its own recovery path (R3-B1: reconstructing a
removed source by `shutil.copytree`ing the WHOLE merged destination back
pulled in an unrelated live sibling — e.g. a Kimi credential directory that
happened to share a parent with a promoted `providers/claude/` — as if it
had always been part of the transaction), never durably recorded a removal
BEFORE performing it (R3-B2: a failed manifest write still let the source
removal proceed), and `restore-v1`'s multi-generation undo used a
per-name "latest backup wins" model that a later generation's OWN restore
could silently shadow (R3-B3).

This module now replaces that shared helper with `TransferEntry` +
`_copy_phase`/`_prune_phase`: every move is FILE-LEVEL from the moment its
candidate file list is computed (before any copy), so a recovery after a
partial removal only ever reconstructs the EXACT files this transaction's
own copy verified — never a directory sweep of whatever else lives beside
them. A removal is only ever attempted after the durable ledger write that
names it has itself already succeeded (`_prune_phase`), and a whole batch's
removals are atomic: if any one fails, every removal this same call already
performed is reversed, never left partially applied. `restore-v1`'s
multi-generation walk (`cli._cmd_migrate_restore_v1`) now snapshots every
name ANY selected generation could touch ONCE, before the walk starts
(`_begin_command_snapshot`/`_revert_to_command_snapshot` below), so a later
generation's failure always reverts to the state the whole command started
from — never an intermediate generation's own overwrite.
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

from ..storage.paths import migration_home
from .backup import BackupManager
from .journal import MigrationJournal
from .registry_copy_step import write_json_atomic
from .report import StepReport
from .verify_copy import VerifyMismatchError, _sha256, copy_only, copy_verified
from .wal import (
    STATE_PENDING,
    STATE_PRUNE_FAILED,
    STATE_SOURCE_PRUNED,
    STATE_VERIFIED,
    TransferLedger,
)


def _log_event(event: str, **details: object) -> None:
    """Best-effort structured log for an error this module has ALREADY
    decided not to escalate further (every `except OSError: pass` below
    calls this instead of swallowing silently) — never raises, and never
    changes what the caller does next. Local import, same reasoning as
    `auto_migrate_boot._log_boot_event`: `orchestrator_text` has zero Qt
    imports, but this module must stay importable (and callable) from a
    plain headless test/process with no orchestrator wiring at all."""
    try:
        from ...orchestrator_text import _log_event as _emit

        _emit(event, **details)
    except Exception:
        return  # swallow-ok: this IS the fallback logging path every
        # other `except OSError` in this module replaced its own swallow
        # with — it has no further sink to report ITS OWN failure to
        # without risking an infinite escalation loop, and it never
        # mutates anything.


_LEGACY_V2_NAME = "v2"
_ARCHIVE_DIR_NAME = "backups"
_MANIFEST_NAME = "manifest.json"

# #574 round8 R8-P1 (production timing finding, 244k-file rehearsal): a
# restore's own entry reconstruction is per-FILE (surgical partial restore
# — see `PromoteV2RootStep.rollback()`'s own docstring for why), unlike
# apply's handful of per-DOMAIN entries. Measured: 3,429/15,747 files in 30
# minutes (~1.9 files/s) — `_copy_phase` was durably rewriting + fsyncing
# the WHOLE (up to ~3.6MB, growing) WAL payload after every SINGLE file.
# `_restore_fsync_batch()` targets a roughly CONSTANT number of checkpoints
# regardless of entry count (never a fixed batch size), so total bytes
# written stays O(n) — not just a smaller constant on the same O(n²) curve
# — as a real production restore's file count grows well past today's
# 15.7k. On-disk WAL FORMAT is unchanged (still one record per file, same
# JSON shape) — only how often it's flushed — so an in-flight WAL from
# before this fix (partially VERIFIED) resumes correctly under it.
_RESTORE_CHECKPOINT_TARGET = 60


def _restore_fsync_batch(entry_count: int) -> int:
    return max(1, -(-entry_count // _RESTORE_CHECKPOINT_TARGET))  # ceil div, no float rounding


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

# #574 round9 (`never_touch_promote_collision`): the one `_V2_TOP_LEVEL_NAMES`
# entry that is itself a home for genuinely LIVE, externally-owned content
# (a provider account's own credential files, #504 R3-B1's "Kimi credential
# directory") rather than something only migration ever writes. A same-path
# copy collision under any OTHER top-level name (`models/`, `state/`, ...) is
# always this transaction's own prior attempt and safe for `copy_only`'s
# duplicate-aside rescue; a collision under `providers/` must instead refuse
# outright (`_refuse_live_collision`, called before `copy_verified` ever
# runs) — never even momentarily move the live file aside.
_STRICT_COLLISION_NAMES: frozenset[str] = frozenset({"providers"})

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


def _promote_v2_root_wal_path(data_home: Path) -> Path:
    # A fixed, stable location `ArchiveV1LegacyStep.apply_copy_only()` also
    # reads directly (#504 round4 B1) — see its own leftover-check
    # docstring — since the two steps hold no reference to each other.
    return data_home / _ARCHIVE_DIR_NAME / "promote-v2-root-wal.json"


def _rel_files(root: Path) -> list[Path]:
    if root.is_file():
        return [Path(".")]
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


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
        except OSError as e:
            _log_event("migration_prune_empty_dir_failed", path=str(target), error=str(e))


def _undo_copied_dest(dest: Path, backup_path: Path | None) -> str | None:
    """Reverse one `copy_verified(src, dest)` call that a batch's later
    entry then failed on: *dest* is removed, then restored from
    *backup_path* if one was taken (a pre-existing merge target) — never a
    bare `shutil.rmtree` of a directory that held content before this
    transaction ever started (#504 B3: the old undo deleted the WHOLE
    destination, including whatever pre-dated the merge). *src* is never
    touched — a copy phase never removes anything at *src*.

    Returns `None` on success, or an error message when restoring
    *backup_path* back onto *dest* fails — a caller unable to put a
    pre-existing destination's OWN prior content back must know about it
    (`_copy_phase` folds this into `CopyOutcome.error`, #504 spec item 5:
    no `except OSError: pass` in a migration/restore/ledger path may swallow
    a failure the caller can't otherwise see)."""
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
    except OSError as e:
        msg = f"could not restore preimage for {dest} from backup {backup_path}: {e}"
        _log_event("migration_undo_preimage_restore_failed", dest=str(dest), error=str(e))
        return msg
    return None


@dataclass(frozen=True, slots=True)
class TransferEntry:
    """One candidate this transaction copies from *src* to *dest* — and, in
    a prune phase, later removes at *src*. *paths* is every relative file
    *src* contains, computed ONCE before any copy ever runs (never derived
    from *dest*'s current content) — the sole source of truth a failed
    removal's recovery reads from, so it can never pull in a sibling that
    was never part of this entry (#504 R3-B1)."""

    name: str
    kind: str  # "file" | "dir"
    src: Path
    dest: Path
    paths: tuple[str, ...] = ()

    def to_ledger(self, digests: dict[str, str] | None = None, state: str = "PRUNED") -> dict:
        # "path" is always `dest` relative to whatever base directory the
        # caller built it from (`archive_root`, for `ArchiveV1LegacyStep`) —
        # which by construction always equals `name` itself, since every
        # `dest` here is `<base>/<name>`. `ArchiveV1LegacyStep.rollback()`
        # reads this key back (archive-relative, so a relocated `backups/`
        # tree still resolves, #504 H2) rather than assuming its caller's
        # `name` convention hasn't changed.
        #
        # `state` defaults to "PRUNED" — every caller of `to_ledger()`
        # builds the FINAL, already-committed manifest entry for something
        # `_prune_phase` has already durably recorded as pruned (#504
        # round4 wal_contract "states": every entry a step's own ledger
        # ever names carries a real state, one of PENDING/VERIFIED/PRUNED/
        # DUPLICATE).
        return {
            "name": self.name,
            "kind": self.kind,
            "paths": list(self.paths),
            "path": self.name,
            "state": state,
            "sha256": digests or {},
            "json": self._committed_json_shape(),
        }

    def _committed_json_shape(self) -> dict[str, bool]:
        """Which of this entry's own ``.json``-named files parse as JSON
        RIGHT NOW, at commit time (#504 round5 R5-H2) — recorded once so a
        later `validate()` can require a promoted/archived target to STAY
        JSON-readable forever WITHOUT assuming every promoted file was
        ever meant to be JSON-shaped: an arbitrary V1 leftover (never
        real registry data) gets promoted right alongside genuine
        registries, and `_committed_target_problems` must never flag one
        of those as broken just because it never was valid JSON to begin
        with."""
        targets = (
            [(self.name, self.dest)]
            if self.kind == "file"
            else [(rel, self.dest / rel) for rel in self.paths]
        )
        shape: dict[str, bool] = {}
        for rel, path in targets:
            if not rel.endswith(".json") or not path.is_file():
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
                shape[rel] = True
            except (OSError, ValueError):
                shape[rel] = False
        return shape

    def restore_source_from_dest(self) -> list[str]:
        """Self-heal ONLY the files `paths` recorded that are no longer at
        `src` — never a sibling entry's files, and never a file still
        correctly in place (#504 round4 B1, Gemini cross-check (B)(1)): a
        removal denied outright (nothing physically touched yet) is
        therefore a no-op here, while a removal that got PARTWAY through
        before raising (some files really gone, others untouched) has
        exactly the gone ones reconstructed from this entry's own already-
        verified `dest` — one at a time, never a directory-level sweep of
        whatever else happens to live under `dest` (#504 R3-B1: the old
        recovery path did ``shutil.copytree(dest, src, dirs_exist_ok=True)``,
        which pulled in unrelated sibling content — e.g. a live provider
        home merged into the same destination directory by an earlier,
        unrelated promotion — as if it had always belonged to THIS entry).
        This entry's OWN `src` is the only thing ever touched — a SIBLING
        entry that already finished removal this same call is never undone
        (#504 round4 `prune_duplicate_only_contract`: a denied removal is
        recorded DUPLICATE, never "restore-then-revert" of everything else).

        Returns every per-file error message encountered (empty on full
        success) — best-effort per file (one failing file must not stop the
        rest from being reconstructed), but never silent: a caller with a
        non-empty result has a `src` that is NOT fully restored and must
        say so (#504 spec item 5), not report the batch as cleanly reverted."""
        errors: list[str] = []
        if self.kind == "file":
            if self.src.exists():
                return errors
            try:
                self.src.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.dest, self.src)
            except OSError as e:
                msg = f"{self.src}: {e}"
                errors.append(msg)
                _log_event(
                    "migration_restore_source_failed",
                    name=self.name,
                    path=str(self.src),
                    error=str(e),
                )
            return errors
        for rel in self.paths:
            s = self.src / rel
            if s.exists():
                continue
            d = self.dest / rel
            try:
                s.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(d, s)
            except OSError as e:
                msg = f"{s}: {e}"
                errors.append(msg)
                _log_event(
                    "migration_restore_source_failed", name=self.name, path=str(s), error=str(e)
                )
        return errors


@dataclass(frozen=True, slots=True)
class CopyOutcome:
    ok: bool
    digests: dict[str, dict[str, str]] = field(default_factory=dict)
    error: str = ""
    # #574 round6 R6-M2: entry name -> relative paths of foreign files
    # `copy_only` moved aside as `.duplicate-<ts>` rather than blocking on
    # forever — empty on the normal path.
    duplicates_kept: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _entry_to_wal(entry: TransferEntry, state: str, sha256: dict[str, str] | None = None) -> dict:
    """#574 round11: *sha256* used to default to a full re-hash of every
    file `entry.src` names (`_source_digests`, now removed) whenever a
    caller didn't pass one explicitly — i.e. on EVERY entry's first
    (`STATE_PENDING`) ledger write, before its own copy has even started.
    That value was never actually read back by anything: `_copy_phase`
    only ever consults a record's `sha256` for `STATE_VERIFIED` /
    `STATE_SOURCE_PRUNED` (via `_verified_target_intact`), and a resumed
    `STATE_PENDING` record falls straight through to a fresh
    `copy_verified()` call regardless of what its `sha256` held
    (`test_resume_from_a_partially_verified_restore_wal_finishes_the_rest`
    already covers a `STATE_PENDING` record with `sha256={}` resuming
    correctly). It was a second full read of the entire source tree, for
    every entry, on every fresh (non-resumed) apply — real cost on a
    ~200k-file backup, for a number that was never used. Defaults to
    `{}` now; every REAL digest still gets written once `copy_verified`/
    `_prune_phase` actually computes one."""
    out = {
        "name": entry.name,
        "kind": entry.kind,
        "src": str(entry.src),
        "dest": str(entry.dest),
        "paths": list(entry.paths),
        "state": state,
        "sha256": sha256 or {},
    }
    return out


def _entry_from_wal(rec: dict) -> TransferEntry:
    return TransferEntry(
        name=rec["name"],
        kind=rec["kind"],
        src=Path(rec["src"]),
        dest=Path(rec["dest"]),
        paths=tuple(rec.get("paths", ())),
    )


def _verified_target_intact(entry: TransferEntry, rec: dict) -> bool:
    """A resumed `VERIFIED` ledger record is a durability CLAIM, not proof
    — recompute every file this entry's own recorded `sha256` names and
    compare against `dest`'s CURRENT content, so a target an earlier
    crashed attempt's own undo already deleted or reverted (#504 round5
    R5-B1) is never skipped as if it were still safely copied. An
    existence probe alone (what `_health_problems` used before) cannot
    tell "copied" from "undone-but-WAL-not-yet-cleared" apart."""
    expected = rec.get("sha256") or {}
    if not expected:
        return False
    if entry.kind == "file":
        # #574 round6 R6-M1: keyed by `entry.src.name` (the bare filename),
        # NOT `entry.name` (the full relative-path name an archive/shared-
        # dir-legacy entry uses, which can contain "/") — `verify_only()`'s
        # own file-branch (`verify_copy.py`) keys its digest dict this same
        # way; using `entry.name` here
        # instead made this guard a dead no-op (silent `None == sha` always
        # False) for any file entry whose name differs from its basename,
        # defeating resumability without ever raising an error about it.
        return entry.dest.is_file() and expected.get(entry.src.name) == _sha256(entry.dest)
    for rel, digest in expected.items():
        target = entry.dest / rel
        if not target.is_file() or _sha256(target) != digest:
            return False
    return True


def _demote_verified_before_undo(
    states: dict[str, dict],
    attempted: list[tuple[TransferEntry, Path | None]],
    ledger: TransferLedger,
) -> None:
    """Before undoing any dest this call already copied, durably strip its
    WAL record back to `PENDING` (#504 round5 R5-B1) — a crash between
    here and this call's own `ledger.clear()` must never leave the WAL
    claiming `VERIFIED` for a target the undo below is about to delete or
    overwrite. Best-effort: `_verified_target_intact` is the real safety
    net on resume regardless of whether this particular write lands, so a
    failure here is logged, never escalated over the original failure
    this call is already unwinding from."""
    changed = False
    for entry, _bp in attempted:
        rec = states.get(entry.name)
        if rec is not None and rec.get("state") == STATE_VERIFIED:
            rec["state"] = STATE_PENDING
            changed = True
    if not changed:
        return
    try:
        ledger.write(states)
    except OSError as e:
        _log_event("migration_undo_demote_failed", error=str(e))


def _refuse_live_collision(entry: TransferEntry) -> None:
    """#574 round9 (`never_touch_promote_collision`): called ONLY for an
    entry whose top level is `_STRICT_COLLISION_NAMES` (a home for
    genuinely LIVE, externally-owned content, #504 R3-B1's "a Kimi
    credential directory") — raises `VerifyMismatchError` on the FIRST
    pre-existing *dest* file whose content differs from *entry*'s own
    source, exactly like the pre-round6 R4-H5 contract, BEFORE
    `copy_verified` (and its round6 duplicate-aside rescue, right for
    every OTHER top-level name but wrong here) ever touches it. A
    same-content match is never a collision — it's the ordinary
    already-copied-once case every resumed entry can hit."""
    if entry.kind == "file":
        rel_pairs = [(entry.src, entry.dest)]
    else:
        rel_pairs = [(entry.src / rel, entry.dest / rel) for rel in entry.paths]
    for src_file, dest_file in rel_pairs:
        if dest_file.is_file() and src_file.is_file() and _sha256(dest_file) != _sha256(src_file):
            raise VerifyMismatchError(
                f"pre-existing content at {dest_file} would be overwritten by this "
                "transaction's own copy — refusing to touch a live, externally-owned home"
            )


def _notify_entry(on_entry: Callable[[str], None] | None, name: str) -> None:
    """Best-effort per-entry progress observer (#574) — never lets a
    caller's callback failure affect the copy/prune phase it's observing."""
    if on_entry is None:
        return
    try:
        on_entry(name)
    except Exception:
        return  # swallow-ok: pure progress notification, not a phase input.


def _copy_phase(
    entries: list[TransferEntry],
    backups: BackupManager,
    step_id: str,
    ledger: TransferLedger,
    *,
    on_entry: Callable[[str], None] | None = None,
    on_file_progress: Callable[[str, int, int, str], None] | None = None,
    fsync_every: int = 1,
) -> CopyOutcome:
    """Copy-verify every entry's `src` into its `dest` via `copy_verified`
    (single call — #504 round4 wal_contract's own seam), backing up any
    PRE-EXISTING `dest` first (#504 B3: a merge into an already-populated
    directory — the kimi-credentials scenario — can be undone byte-for-byte
    instead of guessed at; #504 H3 `restore_collision`: a destination
    recreated since an earlier archive is preserved, not silently
    clobbered). *src* is NEVER touched here.

    #504 round4 T1/T2 — the WAL: every entry in *entries* is durably
    recorded as `PENDING` (#574 round11: with an empty `sha256` — no
    consumer ever reads a `PENDING` record's checksum, so pre-hashing the
    whole source tree there was pure wasted cost, removed) BEFORE this
    call's first byte is copied — or, on a resumed call, `ledger` already
    holds that record from the attempt this
    one is continuing, never re-written from scratch. Each entry then
    durably advances `PENDING -> VERIFIED`, one fsync'd ledger write, before
    the next entry's own first transition is attempted. An entry already at
    `SOURCE_PRUNED` (fully finished by an earlier, interrupted run) is
    skipped entirely — its checksum is read back from the ledger, never
    recomputed.

    On any failure, every entry this call itself newly copied (never one
    resumed already-past `PENDING`) is undone (restored from its own
    backup, or removed if it never existed before), and the ledger is
    cleared — the caller sees `src` fully intact for the whole batch, so a
    retry starts clean.

    *fsync_every* (#574 round8, production timing finding): every entry's
    `states` dict write above is a FULL rewrite of the whole (growing) WAL
    payload, not an append — with the default of 1, `n` entries costs
    O(n) durable writes of up to O(n)-sized payloads each, i.e. O(n²)
    bytes written overall. Fine for the handful of top-level entries a
    normal `apply()` ever sees; ruinous for a caller that reconstructs one
    `TransferEntry` PER FILE (`PromoteV2RootStep.rollback()`'s surgical
    per-file restore, tens of thousands of entries on a real machine — a
    244k-file production rehearsal timed this out at 1800s). Raising
    *fsync_every* durably flushes every Nth entry (and always the last)
    instead of every one — an O(n²/N) reduction, never a correctness
    change: a crash between two batched flushes just means up to N-1
    entries redo their (idempotent, safe) copy+verify on resume, exactly
    like having no WAL at all for those few. The one exception is the
    INITIAL all-PENDING write below, which stays a single unbatched write
    regardless — every entry must be nameable before the first byte of any
    of them is copied (T1), independent of how the per-entry advances are
    later batched.

    *on_file_progress* (#574 round11 item 3): best-effort ``(entry_name,
    files_done, files_total, current_path)`` observer for WITHIN one
    directory entry's own copy+verify — `on_entry` above only ever fires
    once per whole top-level entry, which for a directory holding tens of
    thousands of files left a real pre-migrate-backup rehearsal sitting on
    the SAME entry for 17+ minutes with nothing on screen. Forwarded into
    `copy_verified`'s own `on_file` (see `verify_copy._FileProgressThrottle`
    for the actual every-N-files-or-every-T-seconds throttle) — never
    called for a `file`-kind entry (one file, nothing to show progress
    within)."""
    states = ledger.read()
    for entry in entries:
        if entry.name not in states:
            states[entry.name] = _entry_to_wal(entry, STATE_PENDING)
    try:
        ledger.write(states)
    except OSError as e:
        # Nothing has been copied yet — no `dest` to undo.
        return CopyOutcome(ok=False, error=f"could not write WAL before first copy: {e}")

    digests: dict[str, dict[str, str]] = {}
    duplicates_kept: dict[str, tuple[str, ...]] = {}
    attempted: list[tuple[TransferEntry, Path | None]] = []
    dirty_since_flush = False
    last_index = len(entries) - 1
    for index, entry in enumerate(entries):
        rec = states[entry.name]
        state = rec["state"]
        if state == STATE_SOURCE_PRUNED:
            if _verified_target_intact(entry, rec):
                digests[entry.name] = rec.get("sha256", {})
                _notify_entry(on_entry, entry.name)
                continue
            # #574 round6 R6-B1 (latent BLOCKER): a WAL record claiming
            # SOURCE_PRUNED is a claim, not proof — the old code trusted it
            # blindly here, so a `dest` lost/corrupted by an apply ->
            # rollback -> apply sequence read back as "still a complete
            # copy" with ZERO actual copies left anywhere, reported ok:true.
            # `entry.src` no longer existing is exactly what SOURCE_PRUNED
            # is supposed to mean, so it alone is never proof either way —
            # if it's still there (the WAL claim was stale), treat this
            # exactly like a fresh PENDING entry and fall through to the
            # real copy below; only when BOTH copies are gone is there
            # nothing left to recover from, and that must fail loudly, not
            # silently report success.
            if not entry.src.exists():
                _demote_verified_before_undo(states, attempted, ledger)
                undo_errors = [
                    msg
                    for e2, bp in attempted
                    if (msg := _undo_copied_dest(e2.dest, bp)) is not None
                ]
                ledger.clear()
                error = (
                    f"{entry.name}: WAL claims pruned but target no longer matches its "
                    "recorded checksum, and the source is also gone — missing both, cannot recover"
                )
                if undo_errors:
                    error += "; undo incomplete: " + "; ".join(undo_errors)
                return CopyOutcome(ok=False, error=error)
        if state == STATE_VERIFIED and _verified_target_intact(entry, rec):
            attempted.append((entry, backups.latest_backup(step_id, entry.dest.name)))
            digests[entry.name] = rec.get("sha256", {})
            _notify_entry(on_entry, entry.name)
            continue
        # A `VERIFIED` record whose target no longer matches (#504 round5
        # R5-B1) falls through to a real re-copy below instead of being
        # trusted — `backup_path` below then reflects whatever is
        # actually at `dest` right now, not a stale assumption.
        backup_path = backups.backup(step_id, entry.dest) if entry.dest.exists() else None
        attempted.append((entry, backup_path))
        try:
            if entry.name in _STRICT_COLLISION_NAMES:
                _refuse_live_collision(entry)
            if on_file_progress is None:
                # #574 round11 item 3: omit the `on_file=` kwarg entirely
                # (rather than passing `on_file=None`) when no progress
                # observer is wired — keeps `copy_verified`'s call shape
                # byte-identical to before this feature existed for the
                # overwhelmingly common case, so every existing caller that
                # monkeypatches `copy_verified` with a plain 2-arg fake
                # (this module's own test suite included) keeps working.
                verify = copy_verified(entry.src, entry.dest)
            else:
                verify = copy_verified(
                    entry.src,
                    entry.dest,
                    on_file=lambda done, total, path, _name=entry.name: on_file_progress(
                        _name, done, total, path
                    ),
                )
        except (OSError, VerifyMismatchError) as e:
            _demote_verified_before_undo(states, attempted, ledger)
            undo_errors = [
                msg for e2, bp in attempted if (msg := _undo_copied_dest(e2.dest, bp)) is not None
            ]
            ledger.clear()
            error = str(e)
            if undo_errors:
                error += "; undo incomplete: " + "; ".join(undo_errors)
            return CopyOutcome(ok=False, error=error)
        digests[entry.name] = verify.digests
        if verify.duplicates:
            duplicates_kept[entry.name] = verify.duplicates
            _log_event(
                "migration_duplicate_kept",
                step_id=step_id,
                entry=entry.name,
                paths=list(verify.duplicates),
            )
        rec["state"] = STATE_VERIFIED
        rec["sha256"] = verify.digests
        dirty_since_flush = True
        # #574 round8: durable every Nth entry (and always the last) rather
        # than every one — see this function's own docstring for why.
        if fsync_every <= 1 or (index + 1) % fsync_every == 0 or index == last_index:
            try:
                ledger.write(states)  # durable BEFORE the next entry / prune phase (T2)
            except OSError as e:
                _demote_verified_before_undo(states, attempted, ledger)
                undo_errors = [
                    msg
                    for e2, bp in attempted
                    if (msg := _undo_copied_dest(e2.dest, bp)) is not None
                ]
                ledger.clear()
                error = f"could not record WAL VERIFIED state for {entry.name}: {e}"
                if undo_errors:
                    error += "; undo incomplete: " + "; ".join(undo_errors)
                return CopyOutcome(ok=False, error=error)
            dirty_since_flush = False
        _notify_entry(on_entry, entry.name)
    if dirty_since_flush:
        # Reachable whenever the LAST entry in `entries` took a fast-skip
        # path (already SOURCE_PRUNED/VERIFIED-intact) after an earlier,
        # still-unflushed batched write — the `index == last_index` flush
        # above only fires from the real-copy branch, so this is the
        # guaranteed final flush for that case.
        try:
            ledger.write(states)
        except OSError as e:
            error = f"could not record final WAL state(s): {e}"
            return CopyOutcome(ok=False, error=error)
    return CopyOutcome(ok=True, digests=digests, duplicates_kept=duplicates_kept)


@dataclass(frozen=True, slots=True)
class PruneOutcome:
    pruned: list[TransferEntry]
    ok: bool
    failed_name: str = ""
    error: str = ""
    # #504 round4 R4-B2: name -> relative paths kept in place because their
    # on-disk content changed since this transaction's own verify (a live
    # writer) — never deleted unverified. Not a failure: `ok` can still be
    # True with entries here; the kept file(s) simply remain at `src` for
    # this step's own NEXT candidate scan to pick up and re-copy.
    late_write_kept: dict[str, list[str]] = field(default_factory=dict)
    # name -> its `sha256` dict, captured from the WAL BEFORE this call may
    # have cleared it (#504 round4: a caller building its OWN final
    # manifest after `_prune_phase` returns must read digests from HERE,
    # never re-read the ledger itself — a successful, no-late-write prune
    # clears that ledger as part of ITS OWN return, so a caller re-reading
    # it afterward silently got back `{}` for every entry's checksum,
    # e.g. `restore_corrupt`/`older_archive_integrity`: `validate()` then
    # skipped ALL integrity checking because every recorded digest map was
    # empty).
    digests: dict[str, dict[str, str]] = field(default_factory=dict)
    # #574 round11 R7-M5: True only for a batch-level `write_committed`/
    # `ledger.write` I/O failure — one unrelated to any specific entry's
    # own content, where NOTHING in the batch was actually touched (no
    # removal attempt even started for any of them). `failed_name` still
    # names a representative entry (so a human/log has somewhere to look)
    # but is never recorded as `DUPLICATE` in the manifest — none of the
    # batch has a real duplicate; every entry in it is exactly as it was
    # before the batch started, safely retryable as a normal, un-pruned
    # candidate. `False` (the default) is the existing single-entry
    # `_remove_entry_source` failure shape, unchanged.
    unknown_batch: bool = False


def _prune_failure_summary(action: str, count: int, prune: PruneOutcome) -> str:
    """#504 round4 B1 (Gemini cross-check (B)(1)/(4)): a denied removal is
    never "restore-then-revert" — `prune.failed_name`'s source and target
    both simply remain (recorded DUPLICATE), and every OTHER entry this
    call already pruned stays pruned, never undone alongside it."""
    if prune.unknown_batch:
        # #574 round11 R7-M5: never claim a specific DUPLICATE for a
        # batch-level I/O fault that touched no one entry more than any
        # other — every entry in that batch is untouched, not duplicated.
        return (
            f"{action} copy-verified {count} item(s) but a batch write failed before any "
            f"removal in it was attempted — {prune.failed_name}, every entry in that batch "
            f"left exactly as it was (nothing pruned, nothing duplicated), safely retryable: "
            f"{prune.error}"
        )
    return (
        f"{action} copy-verified {count} item(s) but {prune.failed_name!r}'s source could not "
        f"be removed — recorded DUPLICATE (both source and target copies intact, needs manual "
        f"cleanup, `takkub doctor --storage-layout` surfaces it), never reconstructed: "
        f"{prune.error}"
    )


def _remove_verified_file(src: Path, dest: Path, expected: str | None) -> tuple[bool, str | None]:
    """Remove *src* only if its CURRENT sha256 still matches *expected*
    (what this transaction's own verify step recorded for it) — a file
    that changed on disk since then (a live writer) is NEVER deleted, even
    if it has since "settled" back to something re-verifiable (#504
    round4 R4-B2/H5/H6): the file is always KEPT at `src` and picked up
    again by this step's own NEXT candidate scan, one full apply() later —
    never removed within the SAME call that just detected the drift, so a
    concurrent writer never loses a race against this module's own prune.

    `dest` is still refreshed with the file's current content on a
    mismatch, so the copy target isn't left stale while the file waits to
    be picked up again — best-effort; a failed refresh still just means
    the file is kept.

    Returns `(removed, new_digest)` — *new_digest* is non-None only when a
    refresh just updated *dest*, so the caller can fold the corrected
    digest into whatever it commits."""
    if not src.exists():
        return True, None
    if expected is not None and _sha256(src) == expected:
        src.unlink()
        return True, None
    try:
        copy_only(src, dest)
        return False, _sha256(dest)
    except OSError as e:
        # A failed refresh just means `dest` stays as it was — the file
        # is kept either way. Logged (never a bare swallow) since this
        # did attempt a mutation.
        _log_event("migration_late_write_refresh_failed", src=str(src), error=str(e))
        return False, None


def _entry_digest_key(entry: TransferEntry, rel: str) -> str:
    # A file-kind entry's single digest is keyed by `src.name` (its
    # basename) — matching what `copy_verified`/`verify_only` themselves
    # use — NOT `entry.name`, which for several callers here (rollback's
    # per-file entries, shared-dir-legacy candidates) is a composite
    # ownership name, not a basename.
    return entry.src.name if entry.kind == "file" else rel


def _remove_entry_source(
    entry: TransferEntry, digests: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Remove *entry*'s `src` — a single `_remove(entry.src)` call (whole
    file or directory) in the common case, but ONLY after a read-only
    pass confirms every file it names STILL matches the sha256 this
    transaction's own copy verified. A file that changed on disk since
    then (a live writer) is never bulk-deleted: it — and, for a
    directory entry, ONLY it — is instead removed individually from the
    safe subset (#504 round4 R4-B2/H5/H6), and `dest` is best-effort
    refreshed with its current content so it isn't left stale while the
    file waits to be picked up again by this step's own NEXT candidate
    scan.

    Returns `(removed_rel_paths, kept_rel_paths)` — a non-empty `kept`
    means the file(s) named are still at `src`, never lost, never
    silently deleted unverified."""
    rel_keys = entry.paths if entry.kind == "dir" else (entry.src.name,)
    mismatched: list[str] = []
    for rel in rel_keys:
        p = entry.src / rel if entry.kind == "dir" else entry.src
        if not p.exists():
            continue  # already gone — nothing to verify or remove
        expected = digests.get(_entry_digest_key(entry, rel))
        if expected is None or _sha256(p) != expected:
            mismatched.append(rel)

    # #504 round4 `late_source_file_conservation`: a file that appeared
    # under `entry.src` AFTER this transaction's own candidate scan (never
    # named in `entry.paths` at all) must survive a bulk removal exactly
    # like a hash-mismatched one — a whole-directory `_remove()` has no
    # way to spare it, so its mere presence forces the per-file path
    # below, which only ever touches names this transaction actually
    # verified.
    has_unexpected = (
        entry.kind == "dir"
        and entry.src.is_dir()
        and any(
            p.relative_to(entry.src).as_posix() not in entry.paths
            for p in entry.src.rglob("*")
            if p.is_file()
        )
    )

    if not mismatched and not has_unexpected:
        # The common, expected case: everything still matches what this
        # transaction verified — one bulk removal, exactly as every other
        # step in this ladder does its own cleanup.
        if entry.src.exists():
            _remove(entry.src)
        return (list(entry.paths) if entry.kind == "dir" else [entry.name]), []

    if entry.kind == "file":
        key = entry.src.name
        _, new_digest = _remove_verified_file(entry.src, entry.dest, None)
        if new_digest is not None:
            digests[key] = new_digest
        return [], [entry.name]

    removed: list[str] = []
    kept: list[str] = []
    for rel in entry.paths:
        p = entry.src / rel
        if rel in mismatched:
            if p.exists():
                try:
                    copy_only(p, entry.dest / rel)
                    digests[rel] = _sha256(entry.dest / rel)
                except OSError as e:
                    _log_event("migration_late_write_refresh_failed", src=str(p), error=str(e))
            kept.append(rel)
            continue
        if p.exists():
            p.unlink()
        removed.append(rel)
    return removed, kept


def _prune_phase(
    entries: list[TransferEntry],
    write_committed: Callable[[list[TransferEntry], TransferEntry | None], None],
    ledger: TransferLedger,
    *,
    on_entry: Callable[[str], None] | None = None,
    fsync_every: int = 1,
) -> PruneOutcome:
    """Remove each entry's `src`, one at a time — but only ever AFTER
    `write_committed` (the step's own FINAL manifest, merged with whatever
    this transaction already committed) AND a durable WAL write naming it
    `SOURCE_PRUNED` have both themselves already succeeded (#504 R3-B2: the
    old code called its "record ownership" callback BEFORE removal too,
    but swallowed the callback's own failure and removed the source
    anyway — a failed record must instead refuse the removal it was meant
    to precede). `write_committed(committed_now, failed)` is called with
    the CUMULATIVE list of entries committed so far each time and, once
    known, the ONE entry (or `None`) whose own removal this call denied —
    every entry in `committed_now` other than `failed` is durably PRUNED;
    `failed` itself is DUPLICATE (source and target both still exist).

    #504 round4 T6: an entry the WAL already recorded `SOURCE_PRUNED` by an
    EARLIER, interrupted call (its removal itself didn't finish — e.g.
    killed after N of M files) is retried here by removing whatever of
    `src` still remains, WITHOUT calling `write_committed` again for it —
    that already happened, durably, before this process ever died, and
    doing it again from a FRESH, rescanned (and therefore possibly
    SHRUNK) file list would silently drop the files that removal already
    finished from ownership.

    #504 round4 B1 (Gemini cross-check (B)(1)/(4), `prune_duplicate_only_
    contract`): a denied removal is NEVER "restore-then-revert" — copying
    `dest` back over every entry this SAME call already pruned is exactly
    the fragile second mutation R3-B1 already found unsafe once, and it
    also means a batch's later failure could silently touch files nobody
    asked to touch. Every entry pruned before the failure STAYS pruned
    (its only safe copy already durably verified at `dest`); the ONE
    entry whose own removal was denied keeps BOTH copies and is recorded
    DUPLICATE — fully recoverable, needs an operator's attention
    (`takkub doctor --storage-layout` surfaces it), never silently lost.
    `TransferEntry.restore_source_from_dest()` is still used, but scoped
    ONLY to that one failed entry's own partially-completed removal (a
    directory removal that deleted SOME of its files before raising) —
    never to a sibling that already fully succeeded.

    *fsync_every* (#504 round10, mirroring `_copy_phase`'s own #574
    round8 fix): the per-entry `write_committed` + `ledger.write` calls
    below are each a full-payload rewrite of the whole (growing) manifest
    / WAL — cheap at the default of 1 for a normal apply()/prune()'s
    handful of per-domain entries (unchanged behavior), but the identical
    O(n^2)-bytes-written shape `_copy_phase` had for `PromoteV2RootStep
    .rollback()`'s per-FILE entries: a 244k-file production rehearsal
    measured this side at ~3.5 files/s even after `_copy_phase`'s own
    copy side was fixed (#574 round8/#504 R8-P2). Raising it durably
    records up to N entries' `SOURCE_PRUNED` state — manifest AND WAL,
    together, in ONE call each — BEFORE removing any of that batch's
    sources: the T1 "record before delete" contract still holds, just at
    BATCH granularity instead of per-entry. A crash between a batch's
    durable record and finishing every file's removal in it is safe to
    resume: every name in that batch is already `SOURCE_PRUNED` on disk,
    so the `baseline` resume-retry loop above picks up removing whatever
    of `src` still remains next call — exactly the single-entry case, one
    call later (same as an in-flight WAL from before this fix). A batch
    whose OWN `write_committed`/`ledger.write` call itself fails has
    committed NOTHING for it — none of its entries are marked
    `SOURCE_PRUNED`, none are touched at `src`; they're exactly as if
    this call never reached them, safe to retry via a fresh prune()
    call (only the batch's first entry is named `failed`/DUPLICATE below,
    matching the single-entry contract — the rest are simply unattempted,
    never falsely reported pruned or duplicate)."""
    ledger_states = ledger.read()
    baseline = [
        e for e in entries if ledger_states.get(e.name, {}).get("state") == STATE_SOURCE_PRUNED
    ]
    baseline_names = {e.name for e in baseline}
    late_write_kept: dict[str, list[str]] = {}

    for entry in baseline:
        sha256 = ledger_states.get(entry.name, {}).get("sha256", {})
        try:
            _removed, kept = _remove_entry_source(entry, sha256)
        except OSError as e:
            # Already durably committed SOURCE_PRUNED by an earlier call —
            # a resume retry failing again leaves it exactly as-is, still
            # SOURCE_PRUNED and safely retryable, never demoted to a new
            # DUPLICATE over work this call didn't even attempt fresh.
            _log_event("migration_prune_resume_retry_failed", name=entry.name, error=str(e))
            continue
        if kept:
            late_write_kept[entry.name] = kept
        ledger_states.setdefault(entry.name, {})["sha256"] = sha256
        _notify_entry(on_entry, entry.name)

    newly: list[TransferEntry] = []
    failed: TransferEntry | None = None
    error = ""
    # #574 round11 R7-M5: True only for a batch-level `write_committed`/
    # `ledger.write` failure (an I/O fault unrelated to any one entry's
    # own content — NOTHING in the batch was touched, removal never even
    # started) — as opposed to a genuine per-file `_remove_entry_source`
    # failure, where `failed` really is the one entry whose removal
    # actually raised. Read below (right before the final `return`).
    batch_level_failure = False
    unknown_batch_size = 0
    remaining = [e for e in entries if e.name not in baseline_names]
    last_index = len(remaining) - 1
    batch: list[TransferEntry] = []
    for index, entry in enumerate(remaining):
        batch.append(entry)
        at_boundary = fsync_every <= 1 or len(batch) >= fsync_every or index == last_index
        if not at_boundary:
            continue
        try:
            write_committed([*baseline, *newly, *batch], None)
        except OSError as e:
            failed = batch[0]
            unknown_batch_size = len(batch)
            error = (
                f"could not record removal for a {len(batch)}-entry batch starting at "
                f"{failed.name}: {e}"
            )
            batch_level_failure = True
            batch = []
            break
        # #574 round11 item 6 (R7-H1, reviewer round 7): snapshot every
        # state this batch is about to overwrite BEFORE overwriting it —
        # a mid-batch removal failure below needs to put each UNTOUCHED
        # sibling's WAL record back to exactly this, never leave it at
        # the batch-wide `SOURCE_PRUNED` write two lines down.
        pre_batch_states = dict(ledger_states)
        # Mutate a COPY first, never `ledger_states` itself, until
        # `ledger.write` below actually durably succeeds — otherwise a
        # failed write here would still leave this batch looking
        # `SOURCE_PRUNED` in memory, and the failure-path write further
        # down (which reuses `ledger_states`) could then durably persist
        # that false claim for files whose removal was never attempted.
        candidate_states = dict(ledger_states)
        batch_sha: dict[str, dict[str, str]] = {}
        for e2 in batch:
            sha256 = dict(ledger_states.get(e2.name, {}).get("sha256", {}))
            candidate_states[e2.name] = _entry_to_wal(e2, STATE_SOURCE_PRUNED, sha256)
            batch_sha[e2.name] = sha256
        try:
            ledger.write(candidate_states)
        except OSError as e:
            failed = batch[0]
            unknown_batch_size = len(batch)
            error = (
                f"could not record WAL removal state for a {len(batch)}-entry batch "
                f"starting at {failed.name}: {e}"
            )
            batch_level_failure = True
            batch = []
            break
        ledger_states = candidate_states
        removal_failed = False
        not_yet_attempted: list[TransferEntry] = []
        for pos, e2 in enumerate(batch):
            sha256 = batch_sha[e2.name]
            try:
                _removed, kept = _remove_entry_source(e2, sha256)
            except OSError as e:
                failed, error = e2, str(e)
                # Self-heal ONLY this entry's own partially-completed removal
                # (some of its files really gone, others untouched) — never a
                # sibling's, and never a full reconstruction of an entry that
                # was simply denied outright with nothing touched yet.
                heal_errors = e2.restore_source_from_dest()
                if heal_errors:
                    error += (
                        "; some file(s) could not be restored to their original "
                        "location: " + "; ".join(heal_errors)
                    )
                removal_failed = True
                # #574 round11 item 6 (R7-H1): every sibling AFTER `e2` in
                # this SAME batch never had its own `_remove_entry_source`
                # even attempted — its file(s) are still fully on disk.
                not_yet_attempted = batch[pos + 1 :]
                break
            if kept:
                late_write_kept[e2.name] = kept
            ledger_states[e2.name]["sha256"] = sha256
            newly.append(e2)
            _notify_entry(on_entry, e2.name)
        batch = []
        if removal_failed:
            # The batch-wide `ledger.write(candidate_states)` above
            # already (correctly, per this function's own T1 "record
            # before delete" contract) durably marked the WHOLE batch
            # `SOURCE_PRUNED` before any of its files were removed — a
            # partial-batch failure makes that claim FALSE for every
            # entry that never got its own removal attempt. Revert each
            # one back to its exact pre-batch record (or drop it if this
            # was its first-ever WAL write) so the WAL never durably
            # claims a source is pruned while it's still fully present —
            # the same "a WAL claim is not proof" contract
            # `_verified_target_intact`/R6-B1 already enforce on resume.
            for e2 in not_yet_attempted:
                if e2.name in pre_batch_states:
                    ledger_states[e2.name] = pre_batch_states[e2.name]
                else:
                    ledger_states.pop(e2.name, None)
            break

    digests = {name: dict(rec.get("sha256", {})) for name, rec in ledger_states.items()}
    if failed is None:
        try:
            ledger.write(ledger_states)
        except OSError as e:
            # Best-effort persistence of corrected late-write digests —
            # the manifest `write_committed` calls above already hold the
            # authoritative record for each entry. Logged, never silent.
            _log_event("migration_wal_digest_refresh_failed", error=str(e))
        if not late_write_kept:
            ledger.clear()
        return PruneOutcome(
            pruned=[*baseline, *newly], ok=True, late_write_kept=late_write_kept, digests=digests
        )

    if batch_level_failure:
        # #574 round11 R7-M5: `ledger_states` was never mutated for this
        # batch on either failure path above (both `except` blocks raise
        # BEFORE `ledger_states = candidate_states` ever runs) — it's
        # already exactly what it was before this batch started, nothing
        # to revert. Never call `write_committed` to mark `failed` (really
        # just "the first entry in an untouched batch") as DUPLICATE —
        # that would durably record a false claim for an entry that was
        # never actually touched, the exact defect this fix removes.
        try:
            ledger.write(ledger_states)
        except OSError as e:
            _log_event("migration_wal_write_after_failure_failed", error=str(e))
        return PruneOutcome(
            pruned=[*baseline, *newly],
            ok=False,
            failed_name=f"{failed.name} (+{unknown_batch_size - 1} more in the same batch)"
            if unknown_batch_size > 1
            else failed.name,
            error=error,
            late_write_kept=late_write_kept,
            digests=digests,
            unknown_batch=True,
        )

    ledger_states[failed.name] = _entry_to_wal(
        failed, STATE_PRUNE_FAILED, dict(ledger_states.get(failed.name, {}).get("sha256", {}))
    )
    try:
        write_committed([*baseline, *newly, failed], failed)
    except OSError as e:
        error += f"; could not record DUPLICATE state for {failed.name}: {e}"
        _log_event("migration_prune_duplicate_record_failed", name=failed.name, error=str(e))
    try:
        ledger.write(ledger_states)
    except OSError as e:
        # Best-effort WAL persistence after a failure this call already
        # reports through `error` above — the final manifest
        # `write_committed` call just above is the authoritative record.
        _log_event("migration_wal_write_after_failure_failed", error=str(e))
    return PruneOutcome(
        pruned=[*baseline, *newly],
        ok=False,
        failed_name=failed.name,
        error=error,
        late_write_kept=late_write_kept,
        digests=digests,
    )


def _read_committed_entries(path: Path, key: str) -> list[dict]:
    """Every previously-committed entry already recorded at *path*'s *key*
    list — `[]` when *path* doesn't exist yet (nothing committed so far).
    Raises (never silently treats as empty) when *path* EXISTS but is
    unreadable — #504 R3-H1 `merge_unreadable_prior_manifest`: overwriting
    a corrupt prior record with a fresh, empty one is exactly how a
    previously-recorded promotion's own ownership record used to
    permanently vanish, with its source already gone."""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get(key, []))


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
    except OSError as e:
        # #504 R2-H1 / spec item 5: a listing failure must not silently
        # make every generation vanish from `--list` — an operator (or
        # `restore-v1`'s own generation walk) needs to know SOMETHING is
        # wrong here, not see an empty, all-clear list. `ts=""` (never a
        # real timestamp) rather than `None` — `ArchiveV1LegacyStep
        # .rollback(archive_ts=None)` means "use the latest generation",
        # so a caller that blindly walked this entry's `ts` must fail
        # closed on the lookup instead of silently falling back to latest.
        _log_event("migration_archive_list_failed", path=str(base), error=str(e))
        return [
            {
                "ts": "",
                "path": str(base),
                "created_at": None,
                "archived": [],
                "deleted": [],
                "unreadable": True,
                "error": str(e),
            }
        ]
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
                # #504 round4 R4-B3: a generation whose manifest header was
                # written (`state: PENDING`) but never advanced to
                # `COMPLETE` — a crashed or still-in-progress apply(). A
                # manifest with NO `state` key at all is a pre-round4
                # manifest that finished fully under the old code path,
                # treated as complete for backward compatibility.
                "incomplete": not _manifest_is_complete(manifest),
            }
        )
    return out


def _manifest_is_complete(manifest: dict) -> bool:
    return manifest.get("state", "COMPLETE") == "COMPLETE"


def _find_manifest_by_ts(data_home: Path, ts: str) -> Path | None:
    m = data_home / _ARCHIVE_DIR_NAME / f"v1-archive-{ts}" / _MANIFEST_NAME
    return m if m.is_file() else None


def _find_latest_manifest(data_home: Path) -> Path | None:
    """The most recent generation's manifest — #504 round4 R4-B3: skips a
    generation whose own manifest is still `PENDING` (a crashed or
    in-progress `apply()`, discoverable and resumable via its own WAL, not
    a real generation to restore FROM yet) rather than treating "most
    recently created" as "most recently finished". An explicit
    `--archive <ts>` (`_find_manifest_by_ts`) is unaffected — naming a
    generation directly is never blocked by this."""
    base = data_home / _ARCHIVE_DIR_NAME
    if not base.is_dir():
        return None
    try:
        candidates = sorted(
            (p for p in base.iterdir() if p.is_dir() and p.name.startswith("v1-archive-")),
            reverse=True,
        )
    except OSError:
        return None  # swallow-ok: read-only listing; caller (`rollback()`)
        # already treats "no latest manifest found" as "nothing to
        # restore from", the same outcome as a genuinely empty archive.
    for c in candidates:
        m = c / _MANIFEST_NAME
        if not m.is_file():
            continue
        try:
            manifest = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # swallow-ok: read-only manifest probe while
            # searching for the latest COMPLETE generation; an unreadable
            # one is simply skipped, same as one with no manifest at all.
        if _manifest_is_complete(manifest):
            return m
    return None


def _all_archive_generation_dirs(data_home: Path) -> list[Path]:
    base = data_home / _ARCHIVE_DIR_NAME
    if not base.is_dir():
        return []
    try:
        return sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("v1-archive-"))
    except OSError:
        return []


def _archive_entry_problems(archive_root: Path, manifest: dict) -> list[str]:
    """Every problem (missing file / checksum mismatch) found while checking
    *manifest*'s ``archived`` entries against what's actually under
    *archive_root* — shared by `ArchiveV1LegacyStep.validate()` (H9, which
    only needs to know the archive is intact) and `.rollback()` (#504 H2
    residual: which must refuse the ENTIRE restore, touching nothing at
    `data_home`, the moment ANY archived member turns out missing or
    corrupt). Empty return means the archive is fully intact."""
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


# ---------------------------------------------------------------------------
# #504 R3-B3 — command-level snapshot for a multi-generation `restore-v1`
# ---------------------------------------------------------------------------


class CommandSnapshotError(RuntimeError):
    """`_begin_command_snapshot` could not copy every named entry's
    preimage before a multi-generation `restore-v1` starts touching any of
    them (e.g. ENOSPC) — the caller MUST abort the whole command without
    running any generation's rollback (#504 round4 T5 BLOCKER: the old code
    swallowed this and let the walk proceed with a partial snapshot that
    `_revert_to_command_snapshot` could then only pretend to restore
    from)."""


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    root: Path
    names: tuple[str, ...]


def _begin_command_snapshot(data_home: Path, names: list[str]) -> CommandSnapshot:
    """One immutable snapshot of every *names* entry's CURRENT on-disk
    state, taken ONCE before a multi-generation `restore-v1` touches any of
    them — #504 R3-B3: the old per-name "latest backup wins" model let a
    LATER generation's own restore shadow an earlier one's backup for the
    same top-level name (so undo after a later failure put back the WRONG
    intermediate value, not the state the whole command actually started
    from), and never captured a name nested more than one path segment deep
    at all (`BackupManager.backup()` keys by bare basename only). A missing
    *name* is simply not recorded here — `_revert_to_command_snapshot`
    below removes it again on revert, exactly reproducing "didn't exist
    before this command".

    #504 round4 T5: fail CLOSED. If ANY name's preimage can't be copied
    (ENOSPC, permissions, ...), the WHOLE snapshot is unusable as a
    preimage for ANY name — raises `CommandSnapshotError` instead of
    returning a partial one, and touches nothing at *data_home* itself (the
    snapshot only ever writes under `migration_home()`). The caller must
    abort the entire `restore-v1` command on this — no generation's
    rollback may run without a verified preimage to fall back to."""
    root = migration_home() / "restore-v1-snapshots" / f"{time.time():.6f}"
    entries: dict[str, dict] = {}
    for name in names:
        target = data_home / name
        if not target.exists():
            entries[name] = {"existed": False}
            continue
        dest = root / name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.copytree(target, dest)
                paths = [p.as_posix() for p in _rel_files(target)]
                digests = {p: _sha256(dest / p) for p in paths}
                entries[name] = {"existed": True, "kind": "dir", "paths": paths, "sha256": digests}
            else:
                shutil.copy2(target, dest)
                entries[name] = {"existed": True, "kind": "file", "sha256": {name: _sha256(dest)}}
        except OSError as e:
            _log_event(
                "migration_command_snapshot_failed", name=name, error=str(e), names=list(names)
            )
            raise CommandSnapshotError(
                f"could not snapshot {name!r} before restore-v1 — aborting the whole command "
                f"before touching any generation: {e}"
            ) from e
    write_json_atomic(
        root / _MANIFEST_NAME, {"schema": 1, "created_at": time.time(), "entries": entries}
    )
    return CommandSnapshot(root=root, names=tuple(names))


def _snapshot_entry_problems(saved: Path, entry: dict) -> list[str]:
    """Every problem found verifying one `CommandSnapshot` entry's preimage
    against what `_begin_command_snapshot` recorded for it — empty means
    the preimage at *saved* is intact and safe to restore from."""
    problems: list[str] = []
    digests: dict[str, str] = entry.get("sha256") or {}
    for rel, expected in digests.items():
        target = saved / rel if entry.get("kind") == "dir" else saved
        if not target.is_file():
            problems.append(f"missing: {target}")
            continue
        if _sha256(target) != expected:
            problems.append(f"checksum mismatch: {target}")
    return problems


def _stage_preimage(source: Path, staged: Path) -> str | None:
    """Copy *source* (file or dir) into *staged*, returning an error
    message on failure (never raising) — *staged* is cleaned up both
    before (a leftover from a previous failed attempt) and after a failed
    copy, so it never confuses a later retry or a later candidate
    source."""
    try:
        if staged.is_dir():
            shutil.rmtree(staged)
        elif staged.exists():
            staged.unlink()
        staged.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, staged)
        else:
            shutil.copy2(source, staged)
        return None
    except OSError as e:
        try:
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            elif staged.exists():
                staged.unlink()
        except OSError as e2:
            # Best-effort cleanup of a staging scratch file that was never
            # the real revert target — never mutates it. Logged, never a
            # bare swallow.
            _log_event("migration_revert_stage_cleanup_failed", path=str(staged), error=str(e2))
        return str(e)


def _command_snapshot_backup_fallback(
    backups: BackupManager, step_id: str, name: str, since_ts: float = 0.0
) -> Path | None:
    """#504 round4 `snapshot_revert_middle`: when the command-level
    snapshot's OWN preimage for *name* can't be used (unreadable, or a
    SECOND, independent fault when actually copying from it — a fault
    the sha256 pre-check above it can't foresee), fall back to *step_id*'s
    own per-step `BackupManager` preimage for the SAME name — taken
    automatically the moment this restore-v1 command's own generation
    walk first overwrote it (#504 B3). Returns the backup path only once
    it exists AND is readable (a fresh sha256 computes without raising) —
    never a path this caller would then also fail to read from.

    *since_ts* MUST be the command snapshot's own timestamp
    (`CommandSnapshot.root.name`, i.e. its `created_at`). A multi-
    generation `restore-v1` calls `BackupManager.backup()` once per
    generation it touches, so the NEWEST slot only ever holds whichever
    generation was applied last before a later one failed — not the true
    pre-command state. The OLDEST slot at-or-after *since_ts* is the one
    taken the moment this command's own walk first overwrote *name*,
    i.e. the pre-command preimage this fallback exists to find.

    Keyed by bare basename, same limitation `_begin_command_snapshot`'s own
    docstring already calls out for `BackupManager` — a last-resort
    fallback, never the primary mechanism, so a *name* nested under a
    directory could in principle collide with an unrelated backup sharing
    the same leaf name. Accepted here rather than engineered around: the
    primary command-snapshot path already handles the general case."""
    if not name:
        return None
    candidate = backups.earliest_backup_since(step_id, Path(name).name, since_ts)
    if candidate is None or not candidate.exists():
        return None
    try:
        if candidate.is_dir():
            for rel in _rel_files(candidate):
                _sha256(candidate / rel)
        else:
            _sha256(candidate)
    except OSError:
        return None  # swallow-ok: read-only readability probe on a
        # last-resort fallback candidate — an unreadable candidate is
        # simply not usable; the caller falls back further or reports
        # "unverifiable", never touches `candidate` itself.
    return candidate


def _revert_to_command_snapshot(
    data_home: Path,
    snapshot: CommandSnapshot,
    backups: BackupManager | None = None,
    backup_step_id: str = "archive-v1-legacy",
) -> list[str]:
    """Undo a whole `restore-v1` command by putting every snapshotted name
    back exactly as `_begin_command_snapshot` found it — present names
    copied back, names that were absent before the command removed again.

    #504 round4 T5 BLOCKER: a name is only ever REMOVED from *data_home*
    once its preimage (when one was recorded) has been verified present
    and byte-identical on disk — never unconditionally, before even
    checking whether a preimage exists (the old code's `shutil.rmtree`
    ran first, unconditionally, and only THEN best-effort tried to copy a
    preimage back — so a corrupt/missing snapshot manifest, or a preimage
    that failed to verify, lost the current content with nothing to put
    back, "0 copies").

    #504 round4 B3 (Gemini cross-check): when *backups* is given, a name
    whose OWN command-snapshot preimage can't be used at all (unreadable,
    or fails independently while actually being staged) falls back to
    `backup_step_id`'s per-step `BackupManager` preimage for that same
    name before giving up. A name whose preimage can't be verified via
    EITHER source is left UNTOUCHED at *data_home* and reported as an
    error instead — never removed with nothing safe to put back.

    Returns every error message encountered (empty means every name was
    fully reverted) — never silent, per this module's own "ห้ามกลืน error"
    rule; a caller with a non-empty result has a `data_home` that is only
    PARTIALLY reverted and must say so."""
    manifest_path = snapshot.root / _MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded: dict[str, dict] = manifest.get("entries", {})
    except (OSError, ValueError) as e:
        _log_event("migration_command_snapshot_manifest_unreadable", error=str(e))
        return [f"snapshot manifest unreadable at {manifest_path} — every name left untouched: {e}"]

    # The `BackupManager` fallback below must only ever consider backup
    # slots taken AT OR AFTER this command's own snapshot — never an older
    # one left over from a previous, unrelated restore-v1 (#504 round4
    # `snapshot_revert_middle`).
    since_ts = manifest.get("created_at", 0.0)

    errors: list[str] = []
    for name in snapshot.names:
        current = data_home / name
        entry = recorded.get(name)
        if entry is None:
            errors.append(f"{name}: not recorded in snapshot manifest — left untouched")
            continue
        if not entry.get("existed", False):
            # Genuinely didn't exist before the command — safe to remove
            # whatever the command created, no preimage needed.
            try:
                if current.is_dir():
                    shutil.rmtree(current)
                elif current.exists():
                    current.unlink()
            except OSError as e:
                errors.append(f"{name}: could not remove: {e}")
            continue

        saved = snapshot.root / name
        problems = _snapshot_entry_problems(saved, entry)
        source = None if problems else saved
        if source is None and backups is not None:
            source = _command_snapshot_backup_fallback(backups, backup_step_id, name, since_ts)
        if source is None:
            errors.append(f"{name}: preimage unverifiable ({'; '.join(problems)}) — left untouched")
            continue

        # #504 round4 `snapshot_revert_middle`: a source passing the
        # digest check above only proves it's readable AT THIS MOMENT —
        # the actual copy call right below can STILL independently fail
        # (a second, later fault). Stage the restore into a TEMP sibling
        # first; `current` is only ever removed once that staged copy has
        # fully landed — never before, so a failure here leaves `current`
        # exactly as it was, never a "0 copies" gap.
        staged = current.with_name(f"{current.name}.revert-tmp")
        stage_error = _stage_preimage(source, staged)
        if stage_error is not None and source is saved and backups is not None:
            fallback = _command_snapshot_backup_fallback(backups, backup_step_id, name, since_ts)
            if fallback is not None:
                stage_error = _stage_preimage(fallback, staged)
        if stage_error is not None:
            errors.append(f"{name}: could not stage preimage for revert: {stage_error}")
            continue

        try:
            if current.is_dir():
                shutil.rmtree(current)
            elif current.exists():
                current.unlink()
            os.replace(staged, current)
        except OSError as e:
            errors.append(f"{name}: revert failed: {e}")
    return errors


# ---------------------------------------------------------------------------
# Step — promote a pre-existing nested v2/ root up to the top level
# ---------------------------------------------------------------------------


def _committed_target_problems(path: Path, require_json: bool) -> list[str]:
    """Presence + (when *require_json* says this file WAS JSON at commit
    time, `TransferEntry._committed_json_shape`) JSON-readability, for one
    FINAL, already-pruned promoted target (#504 round5 R5-H2, #568 item
    1) — never a byte/sha256 match against its original migration-time
    snapshot: once `prune()` commits, a promoted target is a live,
    mutable file (a project registry gets written to, credentials get
    refreshed), so only its own basic health can be required forever,
    exactly the same tradeoff `engine._domain_target_problems` already
    makes for the 8 domain steps below this one in the ladder. Never
    inferred from the filename alone — an arbitrary V1 leftover that
    happens to be named ``*.json`` but was never real JSON to begin with
    must not start failing validate() just because this check exists. A
    checksum comparison is intentionally NOT made even mid-transaction
    (`_health_problems` below stays presence-only) — a LATER domain step
    in the SAME pass can legitimately overwrite this exact physical
    target before `_finish_deferred_prune` ever revisits it (e.g.
    `readonly-registries` writing its own default content into the very
    path `promote-v2-root` just moved up)."""
    if not path.is_file():
        return [f"missing: {path}"]
    if require_json:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return [f"unreadable ({e}): {path}"]
    return []


@dataclass
class PromoteV2RootStep:
    step_id: str = "promote-v2-root"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)
    # Best-effort per-entry progress observer (#574) — see
    # `_notify_entry()`'s docstring; wired into `apply_copy_only()`,
    # `prune()`, AND `rollback()`'s own copy/prune calls (`cli.py`'s
    # `restore-v1` sets it before calling either).
    on_entry: Callable[[str], None] | None = None
    # Best-effort WITHIN-entry progress observer (#574 round11 item 3) —
    # `(entry_name, files_done, files_total, current_path)`, forwarded
    # straight into `_copy_phase`'s own `on_file_progress` wherever
    # `on_entry` above is also wired.
    on_file_progress: Callable[[str, int, int, str], None] | None = None

    def _legacy_root(self) -> Path:
        return self.data_home / _LEGACY_V2_NAME

    def _manifest_path(self) -> Path:
        # A fixed name, NOT nested inside `ArchiveV1LegacyStep`'s own
        # timestamped archive dir — this step runs BEFORE that step picks
        # its timestamp, so it needs a location of its own to record which
        # top-level names it moved, for its own `rollback()`/`restore-v1`
        # to read back later. Rewritten (merged) on every apply()/rollback()
        # — there is only ever one "currently promoted" state to describe.
        return self.data_home / _ARCHIVE_DIR_NAME / "promote-v2-root-manifest.json"

    def _pending(self) -> bool:
        # An empty `v2/` shell doesn't count — this step's own job is only
        # to move its CONTENTS up; the (by then empty) folder itself is
        # `ArchiveV1LegacyStep`'s to remove.
        root = self._legacy_root()
        return root.is_dir() and any(root.iterdir())

    def _promote_candidates(self) -> list[Path]:
        root = self._legacy_root()
        if not root.is_dir():
            return []
        try:
            return sorted(root.iterdir())
        except OSError:
            return []  # swallow-ok: read-only listing; apply() re-derives
            # `_pending()` on its own next call rather than trusting this
            # result as authoritative when it can't even be produced.

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

    def _promoted_member_problems(self) -> list[str]:
        """#504 R3 `promoted_member_inventory`: every file EVER recorded as
        promoted must still be present at its target — `_pending()` alone
        only proves the (by-then-empty) `v2/` root is gone, never that
        nothing promoted from it has since disappeared.

        #504 round4 `ledger_damage`: a missing/corrupt/empty manifest is
        ONLY ever legitimate for a machine that never had a legacy `v2/`
        root at all (nothing was ever promoted, nothing to check) — if
        `v2/` still physically exists (even emptied of content, which is
        exactly what a successful `apply()` leaves behind — this step
        never removes the folder itself), a missing/unreadable manifest
        means the ownership record for whatever WAS promoted is gone,
        which must fail validate() loudly, never silently read as "nothing
        to check"."""
        path = self._manifest_path()
        legacy_root_exists = self._legacy_root().is_dir()
        if not path.is_file():
            return ["promote manifest missing"] if legacy_root_exists else []
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return [f"promote manifest unreadable: {e}"]
        if legacy_root_exists and not manifest.get("promoted"):
            return ["promote manifest has no ownership record for a still-present legacy v2/ root"]
        problems: list[str] = []
        for entry in manifest.get("promoted", []):
            name = entry["name"]
            kind = entry.get("kind", "file")
            json_shape = entry.get("json") or {}
            if kind == "file":
                problems.extend(
                    _committed_target_problems(self.data_home / name, json_shape.get(name, False))
                )
                continue
            paths = entry.get("paths")
            if paths is None:
                continue  # pre-#504-fix manifest — no per-file record to check
            for rel in paths:
                problems.extend(
                    _committed_target_problems(
                        self.data_home / name / rel, json_shape.get(rel, False)
                    )
                )
        return problems

    def _wal_path(self) -> Path:
        return _promote_v2_root_wal_path(self.data_home)

    def _rollback_wal_path(self) -> Path:
        return self.data_home / _ARCHIVE_DIR_NAME / "promote-v2-root-rollback-wal.json"

    def apply(self) -> StepReport:
        """Copy-verify (`apply_copy_only()`), then finish with `prune()` in
        the SAME call — the contract every existing direct caller (CLI,
        other ladder steps, the round4 fault harness) already relies on.
        `MigrationEngine`'s own 2-pass ladder barrier (#504 round4 B1/B3)
        instead calls `apply_copy_only()` and `prune()` separately, with a
        whole-ladder validation gate in between — see `engine.py`."""
        report = self.apply_copy_only()
        if not report.ok or report.detail.get("nothing_pending"):
            return report
        return self.prune()

    def apply_copy_only(self) -> StepReport:
        """Copy-verify every legacy `v2/` candidate into its top-level spot
        — durably, via `_copy_phase`'s WAL — WITHOUT touching any V1
        source. Every entry lands `VERIFIED` in the WAL, ready for
        `prune()` to finish later (same call, via `apply()`, or a
        separate one once a caller has independently confirmed the whole
        migration ladder validates clean, #504 round4 B1)."""
        ledger = TransferLedger(self._wal_path(), list_key="promoted", write_fn=write_json_atomic)
        if not self._pending():
            ledger.clear()  # orphaned WAL from a run that finished draining
            # v2/ but crashed before its own `ledger.clear()` — every entry
            # it could still name is already SOURCE_PRUNED, so this is a
            # no-op on real state either way.
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id,
                "apply",
                True,
                "no legacy v2/ root — nothing to promote",
                detail={"nothing_pending": True},
            )

        # #504 round4 T6: resume from a crashed attempt's OWN WAL — never a
        # fresh scan of `_promote_candidates()` — once one exists, so a
        # partial directory removal that shrank what `v2/` currently
        # contains can never shrink what this transaction still owns.
        if ledger.exists():
            new_entries = [_entry_from_wal(rec) for rec in ledger.read().values()]
        else:
            candidates = self._promote_candidates()
            new_entries = []
            for src in candidates:
                dest = self.data_home / src.name
                if src.is_dir():
                    paths = tuple(p.as_posix() for p in _rel_files(src))
                    new_entries.append(TransferEntry(src.name, "dir", src, dest, paths))
                else:
                    new_entries.append(TransferEntry(src.name, "file", src, dest))

        copied = _copy_phase(
            new_entries,
            self.backups,
            self.step_id,
            ledger,
            on_entry=self.on_entry,
            on_file_progress=self.on_file_progress,
        )
        if not copied.ok:
            self.journal.record(self.step_id, "apply", False, copied.error)
            return StepReport(
                self.step_id, "apply", False, f"promote failed, rolled back: {copied.error}"
            )
        detail: dict = {"items": [e.name for e in new_entries]}
        if copied.duplicates_kept:
            detail["duplicates_kept"] = copied.duplicates_kept
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"copy-verified {len(new_entries)} item(s) from {self._legacy_root()}; prune pending",
            detail=detail,
        )

    def prune(self) -> StepReport:
        """Finish `apply_copy_only()` by removing every already-copy-
        verified entry's V1 source — reads the WAL it left behind, so this
        is safe to call standalone (from `apply()`, same call) or later,
        once a caller has independently validated the whole ladder (#504
        round4 B1: `MigrationEngine` never lets this run until every step
        in the SAME pass — including domain steps and every OTHER archive
        generation — has itself validated clean)."""
        ledger = TransferLedger(self._wal_path(), list_key="promoted", write_fn=write_json_atomic)
        if not ledger.exists():
            return StepReport(self.step_id, "apply", True, "nothing pending prune")

        new_entries = [_entry_from_wal(rec) for rec in ledger.read().values()]
        try:
            existing = _read_committed_entries(self._manifest_path(), "promoted")
        except (OSError, ValueError) as e:
            msg = (
                f"existing promote record at {self._manifest_path()} is unreadable — "
                f"refusing to prune V1 data: {e}"
            )
            self.journal.record(self.step_id, "apply", False, msg)
            return StepReport(self.step_id, "apply", False, msg)
        new_names = {e.name for e in new_entries}

        def write_committed(
            committed_now: list[TransferEntry], failed: TransferEntry | None
        ) -> None:
            digests = ledger.read()
            merged = [e for e in existing if e["name"] not in new_names]
            merged += [
                e.to_ledger(
                    digests.get(e.name, {}).get("sha256", {}),
                    state="DUPLICATE" if failed is not None and e.name == failed.name else "PRUNED",
                )
                for e in committed_now
            ]
            write_json_atomic(
                self._manifest_path(), {"schema": 3, "created_at": time.time(), "promoted": merged}
            )

        prune = _prune_phase(new_entries, write_committed, ledger, on_entry=self.on_entry)
        if not prune.ok:
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                _prune_failure_summary("apply", len(new_entries), prune),
                detail={"cleanup_pending": [prune.failed_name]},
            )

        self.journal.record(self.step_id, "apply", True, f"promoted {len(prune.pruned)} item(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"promoted {len(prune.pruned)} item(s) from {self._legacy_root()} to {self.data_home}",
            detail={"items": [e.name for e in prune.pruned]},
        )

    def _health_problems(self) -> list[str]:
        """Copy-target health for whatever THIS pass's own WAL currently
        names (from `apply_copy_only()`) — NOT `_promoted_member_problems()`
        (validate()'s own check): that one reads the FINAL, committed
        manifest, which legitimately does not exist yet the instant right
        after a copy-only pass — `prune()` is the one that writes it (#504
        round4 B1: a still-present legacy `v2/` root with no manifest yet
        is the NORMAL mid-pass state here, never a red flag). Falls back to
        `_promoted_member_problems()` when there is no pending WAL at all
        (nothing copied this pass — whatever the FINAL manifest already
        says stands)."""
        ledger = TransferLedger(self._wal_path(), list_key="promoted", write_fn=write_json_atomic)
        if not ledger.exists():
            return self._promoted_member_problems()
        problems: list[str] = []
        for rec in ledger.read().values():
            entry = _entry_from_wal(rec)
            if entry.kind == "file":
                if not entry.dest.is_file():
                    problems.append(f"missing: {entry.dest}")
                continue
            for rel in entry.paths:
                if not (entry.dest / rel).is_file():
                    problems.append(f"missing: {entry.dest / rel}")
        return problems

    def validate(self) -> StepReport:
        if self._pending():
            return StepReport(self.step_id, "validate", False, "legacy v2/ root still present")
        problems = self._promoted_member_problems()
        if problems:
            return StepReport(
                self.step_id,
                "validate",
                False,
                f"promoted file {problems[0]}",
                detail={"problems": problems},
            )
        return StepReport(self.step_id, "validate", True, "promoted — no legacy v2/ root remains")

    def rollback(self) -> StepReport:
        """Re-create ``v2/`` and move the currently-top-level content this
        step promoted back into it — the pair to `ArchiveV1LegacyStep
        .rollback()`, and what `takkub migrate restore-v1` calls alongside
        it. A no-op when this step never actually promoted anything.

        Restores EXACTLY the relative files the manifest recorded as
        promoted, never the whole current top-level directory — a live
        provider home (or anything else) sharing that top-level name is
        never touched. All-or-nothing per call: a mid-restore failure
        reverses every entry this call already moved back."""
        manifest_path = self._manifest_path()
        # #504 round4 `ledger_damage`: a missing manifest is only ever a
        # legitimate no-op when `v2/` itself never existed either —
        # otherwise the ownership record for whatever WAS promoted is
        # gone, and rollback must refuse rather than silently claim
        # "nothing to undo" over data it has no recovery inventory for.
        if not manifest_path.is_file():
            if self._legacy_root().is_dir():
                msg = (
                    f"promote manifest at {manifest_path} is missing but legacy v2/ root "
                    f"{self._legacy_root()} still exists — refusing to report a clean rollback "
                    "with no recovery inventory"
                )
                self.journal.record(self.step_id, "rollback", False, msg)
                return StepReport(self.step_id, "rollback", False, msg)
            self.journal.record(self.step_id, "rollback", True, "never promoted — nothing to undo")
            return StepReport(
                self.step_id, "rollback", True, "no promote manifest — nothing to undo"
            )
        try:
            existing = _read_committed_entries(manifest_path, "promoted")
        except (OSError, ValueError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"could not read manifest: {e}")
        if not existing and self._legacy_root().is_dir():
            msg = (
                f"promote manifest at {manifest_path} has no ownership record but legacy v2/ "
                f"root {self._legacy_root()} still exists — refusing to report a clean "
                "rollback with no recovery inventory"
            )
            self.journal.record(self.step_id, "rollback", False, msg)
            return StepReport(self.step_id, "rollback", False, msg)

        legacy_root = self._legacy_root()
        ledger = TransferLedger(
            self._rollback_wal_path(), list_key="promoted", write_fn=write_json_atomic
        )

        if ledger.exists():
            # #504 round4 T6: resume from THIS attempt's own WAL, never a
            # fresh rescan — a name already moved back (and so already
            # gone from `data_home`) by a prior, interrupted run would
            # otherwise silently vanish from `entries` on re-scan, along
            # with its still-pending `_prune_phase` removal.
            entries = [_entry_from_wal(rec) for rec in ledger.read().values()]
            prune_map = {}
            for entry in existing:
                paths = entry.get("paths")
                if entry.get("kind", "file") != "file" and paths is not None:
                    prune_map[self.data_home / entry["name"]] = paths
        else:
            entries = []
            prune_map = {}
            for entry in existing:
                name = entry["name"]
                kind = entry.get("kind", "file")
                if kind == "file":
                    src = self.data_home / name
                    if src.exists():
                        entries.append(TransferEntry(name, "file", src, legacy_root / name))
                    continue
                paths = entry.get("paths")
                if paths is None:
                    # Pre-#504-fix manifest (schema 1) — no per-file record
                    # exists to restore surgically. Skip rather than guess
                    # at moving the whole (possibly-merged) top-level dir
                    # back.
                    continue
                base = self.data_home / name
                prune_map[base] = paths
                for rel in paths:
                    s = base / rel
                    if s.is_file():
                        entries.append(
                            TransferEntry(f"{name}/{rel}", "file", s, legacy_root / name / rel)
                        )

        if not entries:
            ledger.clear()
            self.journal.record(self.step_id, "rollback", True, "nothing left to restore")
            return StepReport(self.step_id, "rollback", True, "nothing left to restore")

        def write_committed(
            restored_now: list[TransferEntry], _failed: TransferEntry | None
        ) -> None:
            restored_names = {e.name for e in restored_now}
            new_promoted: list[dict] = []
            for entry in existing:
                name = entry["name"]
                kind = entry.get("kind", "file")
                if kind == "file":
                    if name in restored_names:
                        continue
                    new_promoted.append(entry)
                    continue
                paths = entry.get("paths")
                if paths is None:
                    new_promoted.append(entry)
                    continue
                remaining = [rel for rel in paths if f"{name}/{rel}" not in restored_names]
                if remaining:
                    new_promoted.append({**entry, "paths": remaining})
            write_json_atomic(
                manifest_path, {"schema": 3, "created_at": time.time(), "promoted": new_promoted}
            )

        copied = _copy_phase(
            entries,
            self.backups,
            self.step_id,
            ledger,
            on_entry=self.on_entry,
            on_file_progress=self.on_file_progress,
            fsync_every=_restore_fsync_batch(len(entries)),
        )
        if not copied.ok:
            self.journal.record(self.step_id, "rollback", False, copied.error)
            return StepReport(self.step_id, "rollback", False, f"restore failed: {copied.error}")

        prune = _prune_phase(
            entries,
            write_committed,
            ledger,
            on_entry=self.on_entry,
            fsync_every=_restore_fsync_batch(len(entries)),
        )
        if not prune.ok:
            self.journal.record(self.step_id, "rollback", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "rollback",
                False,
                _prune_failure_summary("rollback", len(entries), prune),
                detail={"cleanup_pending": [prune.failed_name]},
            )

        for base, paths in prune_map.items():
            _prune_empty_dirs(base, paths)

        restored = [str(e.dest.relative_to(legacy_root)) for e in prune.pruned]
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
    # Best-effort per-entry progress observer (#574) — wired into
    # `apply_copy_only()`, `prune()`, AND `rollback()`'s own copy/prune
    # calls (`cli.py`'s `restore-v1` sets it before calling either).
    on_entry: Callable[[str], None] | None = None
    # Best-effort WITHIN-entry progress observer (#574 round11 item 3) —
    # see `PromoteV2RootStep.on_file_progress`'s twin docstring.
    on_file_progress: Callable[[str, int, int, str], None] | None = None

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
        a static skip-list could enumerate, so this reads the real registry
        instead of guessing a naming convention. Best-effort: a missing/
        corrupt registry just means nothing extra gets protected here.

        B4-residual: `config_dir` need not sit directly at `data_home`'s top
        level — a registered home can be nested arbitrarily deep. This adds
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
                continue  # swallow-ok: read-only path computation on one
                # registry entry; a malformed `config_dir` just protects
                # nothing extra, it never touches disk.
            if rel.parts:
                out.add(rel.parts[0])
        return out

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
        """#504 round4 R4-H4: a shared-dir glob match must pass the SAME
        never-touch / registered-account-home check every top-level
        candidate does (`_named_account_home_names()`), applied to its own
        top-level path segment — a named account whose `config_dir` is
        registered somewhere under `agents/` or `projects/` must never be
        swept up here just because it happens to match one of these
        globs."""
        if not self.data_home.is_dir():
            return []
        protected = self._named_account_home_names()
        out: list[Path] = []
        for pattern in _SHARED_DIR_LEGACY_GLOBS:
            try:
                matches = sorted(p for p in self.data_home.glob(pattern) if p.is_file())
            except OSError:
                continue  # swallow-ok: read-only listing for one glob
                # pattern; `_pending()`/`apply()` simply see fewer
                # candidates, never a false "nothing to archive".
            for p in matches:
                rel = p.relative_to(self.data_home)
                if rel.parts and rel.parts[0] in protected:
                    continue
                out.append(p)
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

    def _wal_path(self) -> Path:
        # A fixed name (not itself inside a timestamped generation dir) so
        # a resumed process can find it without already knowing which
        # generation a crashed attempt picked — the chosen `archive_root`
        # is instead recorded in the WAL's own `meta` (#504 round4 T6).
        return self._archive_base() / "archive-v1-legacy-wal.json"

    def apply(self) -> StepReport:
        """Copy-verify (`apply_copy_only()`), then finish with `prune()` in
        the SAME call — the contract every existing direct caller already
        relies on. `MigrationEngine`'s own 2-pass ladder barrier (#504
        round4 B1/B3) calls these two separately instead, gated by a
        whole-ladder validation in between — see `engine.py`."""
        report = self.apply_copy_only()
        if not report.ok or report.detail.get("nothing_pending"):
            return report
        return self.prune()

    def _pending_archive_root(self) -> Path | None:
        """The `archive_root` THIS step's own (not-yet-pruned) WAL is
        currently building, if any — `_stale_generation_problems()` below
        must never judge this ONE generation against "is it COMPLETE yet",
        since that only ever becomes true once `prune()` finishes it
        (#504 round4 `barrier_old_generation`: only a generation with NO
        live WAL pointing at it is genuinely stale/orphaned)."""
        ledger = TransferLedger(self._wal_path(), list_key="archived", write_fn=write_json_atomic)
        if not ledger.exists():
            return None
        try:
            return Path(ledger.read_meta()["archive_root"])
        except (KeyError, OSError, ValueError):
            return None  # swallow-ok: read-only WAL meta probe — an
            # unreadable/malformed WAL just means "no pending generation
            # known", never touches anything; `_stale_generation_problems`
            # below still independently validates every generation on disk.

    def _stale_generation_problems(self) -> list[str]:
        """Every integrity problem found in archive generations OTHER than
        the one THIS step's own WAL is currently building (see
        `_pending_archive_root()`) — `MigrationEngine`'s ladder barrier
        (#504 round4 B1) calls this BEFORE any step's `prune()` runs, so a
        pre-existing generation left corrupt/incomplete by a PRIOR,
        unrelated run blocks every step's prune this pass, never the
        generation THIS SAME pass just started (still legitimately
        mid-flight until this step's own `prune()` finishes it)."""
        active = self._pending_archive_root()
        problems: list[str] = []
        for gen in _all_archive_generation_dirs(self.data_home):
            if active is not None and gen == active:
                continue
            manifest_path = gen / _MANIFEST_NAME
            if not manifest_path.is_file():
                problems.append(f"archive manifest missing for generation {gen.name}")
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                problems.append(f"archive manifest unreadable ({gen.name}): {e}")
                continue
            if not manifest.get("archived") and any(
                p.name not in (_MANIFEST_NAME, _LEGACY_V2_NAME) for p in gen.iterdir()
            ):
                problems.append(
                    f"archive manifest for generation {gen.name} has no ownership record for "
                    "content actually present in its directory"
                )
                continue
            if not _manifest_is_complete(manifest):
                problems.append(
                    f"archive generation {gen.name} is incomplete (crashed or in-progress)"
                )
                continue
            problems.extend(_archive_entry_problems(gen, manifest))
        return problems

    def _health_problems(self) -> list[str]:
        """Copy-target health, independent of whether `prune()` has run
        yet — see `PromoteV2RootStep._health_problems()`'s twin docstring.
        `MigrationEngine`'s ladder barrier calls THIS, never `validate()`,
        to gate a pass's deferred prune."""
        return self._stale_generation_problems()

    def apply_copy_only(self) -> StepReport:
        """Copy-verify every V1 leftover into a NEW archive generation —
        durably, via `_copy_phase`'s WAL — WITHOUT removing any V1 source,
        deleting #504 item 5's junk, or touching the (by-then-empty)
        legacy `v2/` marker. `prune()` finishes all of that later (same
        call, via `apply()`, or a separate one once a caller has
        independently confirmed the whole migration ladder validates
        clean, #504 round4 B1)."""
        ledger = TransferLedger(self._wal_path(), list_key="archived", write_fn=write_json_atomic)
        if not self._pending():
            ledger.clear()
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id,
                "apply",
                True,
                "no V1 leftovers found — nothing to archive",
                detail={"nothing_pending": True},
            )

        legacy_root = self._legacy_root()
        if legacy_root.is_dir():
            # #504 round4 B1: a file `promote-v2-root`'s OWN WAL already
            # names VERIFIED (or SOURCE_PRUNED) is NOT "un-promoted" —
            # `PromoteV2RootStep.apply_copy_only()` has already durably
            # copy-verified it; only its PRUNE (deferred to the SAME
            # ladder barrier this step's own prune is deferred to) is
            # still pending. Without this, the 2-pass split deadlocks:
            # promote can't prune until archive validates clean, but
            # archive refuses to even copy while ANY un-pruned file sits
            # under legacy_root — which, mid-pass, is every one of them.
            promote_states = {}
            promote_wal = _promote_v2_root_wal_path(self.data_home)
            if promote_wal.is_file():
                try:
                    promote_states = TransferLedger(promote_wal, list_key="promoted").read()
                except (OSError, ValueError):
                    promote_states = {}

            def _promoted_covers(rel: Path) -> bool:
                rec = promote_states.get(rel.parts[0])
                return rec is not None and rec.get("state") in (
                    STATE_VERIFIED,
                    STATE_SOURCE_PRUNED,
                )

            leftover = next(
                (
                    p
                    for p in legacy_root.rglob("*")
                    if p.is_file() and not _promoted_covers(p.relative_to(legacy_root))
                ),
                None,
            )
            if leftover is not None:
                # #504 B2: refuse the whole apply rather than delete a
                # `promote-v2-root` that never finished (or was
                # interrupted) mid-copy — real un-promoted data may still
                # be here, and deleting it would violate rule #1 ("ย้าย ไม่
                # ลบ") outright.
                msg = (
                    f"legacy v2/ root at {legacy_root} still holds un-promoted file(s) "
                    f"(e.g. {leftover.relative_to(legacy_root)}) — refusing to delete; "
                    "promote-v2-root must finish (or be rolled back) first"
                )
                self.journal.record(self.step_id, "apply", False, msg)
                return StepReport(self.step_id, "apply", False, msg)

        # #504 round4 T1/T6: resume from a crashed attempt's own WAL
        # (reusing the SAME `archive_root` generation it already started
        # writing into) rather than picking a fresh timestamp and
        # rescanning — a rescan after a partial removal would see a
        # SMALLER candidate list than what that attempt already promised.
        if ledger.exists():
            meta = ledger.read_meta()
            archive_root = Path(meta["archive_root"])
            new_entries = [_entry_from_wal(rec) for rec in ledger.read().values()]
        else:
            ts = f"{time.time():.6f}"
            archive_root = self._archive_base() / f"v1-archive-{ts}"
            candidates = self._archive_candidates() + self._shared_dir_legacy_candidates()
            new_entries = []
            for src in candidates:
                name = src.relative_to(self.data_home).as_posix()
                dest = archive_root / name
                if src.is_dir():
                    paths = tuple(p.as_posix() for p in _rel_files(src))
                    new_entries.append(TransferEntry(name, "dir", src, dest, paths))
                else:
                    new_entries.append(TransferEntry(name, "file", src, dest))
            try:
                ledger.write(
                    {e.name: _entry_to_wal(e, STATE_PENDING) for e in new_entries},
                    meta={"archive_root": str(archive_root)},
                )
            except OSError as e:
                msg = f"could not write WAL before first copy: {e}"
                self.journal.record(self.step_id, "apply", False, msg)
                return StepReport(self.step_id, "apply", False, msg)
        manifest_path = archive_root / _MANIFEST_NAME
        if not manifest_path.is_file():
            # #504 round4 R4-B3: the manifest header (generation id + a
            # `PENDING` state) is written durably BEFORE this generation's
            # first file is copied — `list_v1_archives()`/`rollback()`
            # below then know this generation EXISTS but isn't finished
            # even if this process dies before writing anything else. A
            # write failure here means NOTHING has been copied yet — fail
            # closed with a normal StepReport, never an unhandled
            # exception (#504 round4 `manifest_write_archive_crash`).
            try:
                write_json_atomic(
                    manifest_path,
                    {
                        "schema": 2,
                        "created_at": time.time(),
                        "state": "PENDING",
                        "archived": [],
                        "legacy_v2_marker_archived": False,
                        "deleted": [],
                    },
                )
            except OSError as e:
                msg = f"could not write archive manifest header before first copy: {e}"
                self.journal.record(self.step_id, "apply", False, msg)
                return StepReport(self.step_id, "apply", False, msg)

        copied = _copy_phase(
            new_entries,
            self.backups,
            self.step_id,
            ledger,
            on_entry=self.on_entry,
            on_file_progress=self.on_file_progress,
        )
        if not copied.ok:
            # `_copy_phase` already reversed every `dest` it touched — any
            # directory structure left under a fresh `archive_root` (its
            # own `mkdir(parents=True)` calls) is empty scaffolding, never
            # user data, so it's safe to discard entirely.
            try:
                shutil.rmtree(archive_root, ignore_errors=True)
                base = self._archive_base()
                if base.is_dir() and not any(base.iterdir()):
                    base.rmdir()
            except OSError as e:
                _log_event(
                    "migration_archive_scaffolding_cleanup_failed",
                    archive_root=str(archive_root),
                    error=str(e),
                )
            self.journal.record(self.step_id, "apply", False, copied.error)
            return StepReport(
                self.step_id, "apply", False, f"archive failed, rolled back: {copied.error}"
            )
        detail: dict = {"archive_root": str(archive_root)}
        if copied.duplicates_kept:
            detail["duplicates_kept"] = copied.duplicates_kept
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"copy-verified {len(new_entries)} item(s) into {archive_root}; prune pending",
            detail=detail,
        )

    def prune(self) -> StepReport:
        """Finish `apply_copy_only()` by removing every already-copy-
        verified V1 source, retiring the (by-then-empty) legacy `v2/`
        marker, and deleting #504 item 5's junk outright — reads the WAL
        `apply_copy_only()` left behind, so this is safe to call standalone
        (from `apply()`, same call) or later, once a caller has
        independently validated the whole ladder (#504 round4 B1)."""
        ledger = TransferLedger(self._wal_path(), list_key="archived", write_fn=write_json_atomic)
        if not ledger.exists():
            return StepReport(self.step_id, "apply", True, "nothing pending prune")

        meta = ledger.read_meta()
        archive_root = Path(meta["archive_root"])
        new_entries = [_entry_from_wal(rec) for rec in ledger.read().values()]
        manifest_path = archive_root / _MANIFEST_NAME
        legacy_root = self._legacy_root()

        def write_committed(
            committed_now: list[TransferEntry], failed: TransferEntry | None
        ) -> None:
            digests = ledger.read()
            archived = [
                e.to_ledger(
                    digests.get(e.name, {}).get("sha256", {}),
                    state="DUPLICATE" if failed is not None and e.name == failed.name else "PRUNED",
                )
                for e in committed_now
            ]
            write_json_atomic(
                manifest_path,
                {
                    "schema": 2,
                    "created_at": time.time(),
                    "state": "PENDING",  # #504 round4 R4-B3: only set COMPLETE
                    # once this prune() has fully finished below.
                    "archived": archived,
                    "legacy_v2_marker_archived": False,
                    "deleted": [],
                },
            )

        prune = _prune_phase(new_entries, write_committed, ledger, on_entry=self.on_entry)
        if not prune.ok:
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                _prune_failure_summary("apply", len(new_entries), prune),
                detail={"cleanup_pending": [prune.failed_name]},
            )

        done = prune.pruned
        # #504 round4: NEVER re-read `ledger` here — a successful prune with
        # no late writes already cleared it (`_prune_phase`'s own return),
        # so a fresh read back would silently see `{}` and every checksum
        # below would go missing. `prune.digests` is the same WAL state,
        # captured before that clear.
        final_digests = prune.digests

        def digest_for(name: str) -> dict[str, str]:
            return final_digests.get(name, {})

        archived_legacy_marker = False
        if legacy_root.is_dir():
            (archive_root / _LEGACY_V2_NAME).mkdir(parents=True, exist_ok=True)
            try:
                _rmdir_tree(legacy_root)
                archived_legacy_marker = True
            except OSError as e:
                # The archive move above already fully succeeded — every
                # entry's source is gone and every dest verified — so
                # undoing `done` here would delete the ONLY remaining copy
                # of that data over a failure scoped to the legacy v2/
                # marker alone. Record what actually archived and report
                # the marker problem separately instead.
                write_json_atomic(
                    manifest_path,
                    {
                        "schema": 2,
                        "created_at": time.time(),
                        "state": "PENDING",  # legacy v2/ marker not archived yet
                        "archived": [e.to_ledger(digest_for(e.name)) for e in done],
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

        write_json_atomic(
            manifest_path,
            {
                "schema": 2,
                "created_at": time.time(),
                "state": "COMPLETE",  # #504 round4 R4-B3
                "archived": [e.to_ledger(digest_for(e.name)) for e in done],
                "legacy_v2_marker_archived": archived_legacy_marker,
                "deleted": [],
            },
        )

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
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["deleted"] = deleted
                if delete_failures:
                    manifest["delete_failures"] = delete_failures
                write_json_atomic(manifest_path, manifest)
            except OSError as e:
                _log_event(
                    "migration_archive_deleted_manifest_update_failed",
                    manifest_path=str(manifest_path),
                    error=str(e),
                )

        self.journal.record(
            self.step_id,
            "apply",
            True,
            f"archived {len(done)} item(s), deleted {len(deleted)} junk item(s)",
        )
        summary = (
            f"archived {len(done)} V1 item(s) into {archive_root}, deleted {len(deleted)} "
            "junk item(s)"
        )
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
        # #504 H9 / R2-H9 / R3-H2: "nothing left to archive" alone used to
        # be treated as proof the LATEST archive is intact — recompute the
        # sha256 of every file every generation recorded and compare, and
        # ALSO refuse a generation whose manifest.json is missing entirely
        # (the old `_find_all_manifests`-based loop silently filtered those
        # out instead of failing on them), so silent corruption/deletion of
        # ANY archive generation turns validate() red instead of staying
        # green forever.
        for gen in _all_archive_generation_dirs(self.data_home):
            manifest_path = gen / _MANIFEST_NAME
            if not manifest_path.is_file():
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archive manifest missing for generation {gen.name}",
                )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archive manifest unreadable ({gen.name}): {e}",
                )
            # #504 round4 `ledger_damage`: a manifest with NO "archived"
            # entries but real files sitting in its own generation
            # directory (besides manifest.json itself) describes NOTHING
            # about content that is physically there — that's a lost
            # ownership record, not a clean/empty generation.
            if not manifest.get("archived") and any(
                p.name not in (_MANIFEST_NAME, _LEGACY_V2_NAME) for p in gen.iterdir()
            ):
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archive manifest for generation {gen.name} has no ownership record for "
                    "content actually present in its directory",
                )
            problems = _archive_entry_problems(gen, manifest)
            if problems:
                return StepReport(
                    self.step_id,
                    "validate",
                    False,
                    f"archived file {problems[0]} (generation {gen.name})",
                )
        return StepReport(self.step_id, "validate", True, "no V1 leftovers remain")

    def rollback(self, archive_ts: str | None = None) -> StepReport:
        """`takkub migrate restore-v1`'s core: COPY every archived item back
        to its original top-level spot (never move — the archive itself is
        never deleted or expired by this codebase, #504 item 2) from ONE
        archive generation's manifest — the latest, unless *archive_ts*
        names a specific ``v1-archive-<ts>`` generation. #504 item 5's
        deleted junk is NOT recoverable by design — that's the whole point
        of deleting it outright instead of archiving it.

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
                # exist is a caller error, not "nothing to restore".
                msg = f"no v1-archive-{archive_ts} generation found"
                self.journal.record(self.step_id, "rollback", False, msg)
                return StepReport(self.step_id, "rollback", False, msg)
            # #504 round4 `ledger_damage`: a generation DIRECTORY existing
            # with no usable manifest (deleted/corrupt/empty) is NOT the
            # same as "no archive at all" — refusing to report a clean
            # rollback with no recovery inventory for it.
            broken: list[Path] = []
            for g in _all_archive_generation_dirs(self.data_home):
                m = g / _MANIFEST_NAME
                try:
                    manifest_ok = m.is_file() and bool(json.loads(m.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    manifest_ok = False
                if not manifest_ok:
                    broken.append(g)
            if broken:
                msg = (
                    f"{len(broken)} archive generation(s) have no usable manifest — refusing "
                    f"to report a clean rollback with no recovery inventory: "
                    f"{', '.join(g.name for g in broken)}"
                )
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
        # #504 round4 `ledger_damage`: same as `validate()` — a manifest
        # with no "archived" entries but real content actually sitting in
        # its own generation directory has lost its ownership record; it
        # must refuse, not report a clean "restored 0 items".
        if not manifest.get("archived") and any(
            p.name not in (_MANIFEST_NAME, _LEGACY_V2_NAME) for p in archive_root.iterdir()
        ):
            msg = (
                f"archive manifest at {manifest_path} has no ownership record for content "
                f"actually present in {archive_root} — refusing to restore"
            )
            self.journal.record(self.step_id, "rollback", False, msg)
            return StepReport(self.step_id, "rollback", False, msg)
        # #504 H2 residual: fail CLOSED — refuse the whole restore, touching
        # nothing at `data_home`, the moment any archived member is missing
        # or corrupt.
        problems = _archive_entry_problems(archive_root, manifest)
        if problems:
            msg = (
                f"archive at {archive_root} is incomplete/corrupt "
                f"({len(problems)} problem(s)) — refusing to restore: {'; '.join(problems[:5])}"
            )
            self.journal.record(self.step_id, "rollback", False, msg)
            return StepReport(self.step_id, "rollback", False, msg, detail={"problems": problems})

        # #504 round4 `never_touch_restore_registered`: a name a LIVE
        # account now registers (`user-profiles.json`) may not have been
        # registered yet back when this generation was archived — restore
        # must still never write into it, same protection `apply()`'s own
        # `_archive_candidates()` already gives the CURRENT registry.
        protected_top_segments = self._named_account_home_names()
        skipped_registered: list[str] = []
        entries: list[TransferEntry] = []
        for entry in manifest.get("archived", []):
            name = entry["name"]
            if name.split("/", 1)[0] in protected_top_segments:
                skipped_registered.append(name)
                continue
            rel = entry.get("path")
            src = archive_root / rel if rel is not None else Path(entry.get("path", ""))
            entries.append(TransferEntry(name, "file", src, self.data_home / name))

        # Copy-only — the archive itself is never removed by a restore, so
        # (unlike `apply()`'s promote/archive side) there is no shrinking-
        # rescan hazard here: `entries` is rebuilt from the SAME immutable
        # manifest on every call, so a plain per-generation WAL (cleared on
        # success) is enough for the T1/T2 durability contract without
        # needing resume-from-ledger.
        ledger = TransferLedger(
            migration_home() / "restore-v1-copy-wal" / f"{archive_root.name}.json",
            write_fn=write_json_atomic,
        )
        copied = _copy_phase(
            entries,
            self.backups,
            self.step_id,
            ledger,
            on_entry=self.on_entry,
            on_file_progress=self.on_file_progress,
            fsync_every=_restore_fsync_batch(len(entries)),
        )
        ledger.clear()
        if not copied.ok:
            self.journal.record(self.step_id, "rollback", False, copied.error)
            return StepReport(self.step_id, "rollback", False, f"restore failed: {copied.error}")

        restored = [e.name for e in entries]
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
            detail={
                "archive_root": str(archive_root),
                "restored": restored,
                "skipped_registered": skipped_registered,
            },
        )
