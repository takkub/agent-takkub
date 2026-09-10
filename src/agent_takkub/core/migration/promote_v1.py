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
from .verify_copy import VerifyMismatchError, _sha256, copy_verified


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
        pass


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
        except OSError:
            pass


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

    def to_ledger(self, digests: dict[str, str] | None = None) -> dict:
        # "path" is always `dest` relative to whatever base directory the
        # caller built it from (`archive_root`, for `ArchiveV1LegacyStep`) —
        # which by construction always equals `name` itself, since every
        # `dest` here is `<base>/<name>`. `ArchiveV1LegacyStep.rollback()`
        # reads this key back (archive-relative, so a relocated `backups/`
        # tree still resolves, #504 H2) rather than assuming its caller's
        # `name` convention hasn't changed.
        out: dict = {
            "name": self.name,
            "kind": self.kind,
            "paths": list(self.paths),
            "path": self.name,
        }
        if digests:
            out["sha256"] = digests
        return out

    def restore_source_from_dest(self) -> list[str]:
        """Per-file reconstruction of `src` from the already-verified
        `dest` — copies EXACTLY the files `paths` recorded before this
        entry's own copy ever ran, one at a time, never a directory-level
        sweep of whatever else happens to live under `dest` (#504 R3-B1:
        the old recovery path did ``shutil.copytree(dest, src,
        dirs_exist_ok=True)``, which pulled in unrelated sibling content —
        e.g. a live provider home merged into the same destination
        directory by an earlier, unrelated promotion — as if it had always
        belonged to THIS entry). Safe to call unconditionally regardless of
        how much of `src` a failed removal actually managed to delete
        before raising — every recorded file is simply (re)written from
        `dest`.

        Returns every per-file error message encountered (empty on full
        success) — best-effort per file (one failing file must not stop the
        rest from being reconstructed), but never silent: a caller with a
        non-empty result has a `src` that is NOT fully restored and must
        say so (#504 spec item 5), not report the batch as cleanly reverted."""
        errors: list[str] = []
        if self.kind == "file":
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


def _copy_phase(entries: list[TransferEntry], backups: BackupManager, step_id: str) -> CopyOutcome:
    """Copy-verify every entry's `src` into its `dest`, backing up any
    PRE-EXISTING `dest` first (#504 B3: a merge into an already-populated
    directory — the kimi-credentials scenario — can be undone byte-for-byte
    instead of guessed at; #504 H3 `restore_collision`: a destination
    recreated since an earlier archive is preserved, not silently
    clobbered). *src* is NEVER touched here. On any failure, every `dest`
    attempted so far in THIS call is undone (restored from its own backup,
    or removed if it never existed before) — the caller sees `src` fully
    intact for the whole batch, so a retry starts clean."""
    digests: dict[str, dict[str, str]] = {}
    attempted: list[tuple[TransferEntry, Path | None]] = []
    for entry in entries:
        backup_path = backups.backup(step_id, entry.dest) if entry.dest.exists() else None
        attempted.append((entry, backup_path))
        try:
            verify = copy_verified(entry.src, entry.dest)
            digests[entry.name] = verify.digests
        except (OSError, VerifyMismatchError) as e:
            undo_errors = [
                msg for e2, bp in attempted if (msg := _undo_copied_dest(e2.dest, bp)) is not None
            ]
            error = str(e)
            if undo_errors:
                error += "; undo incomplete: " + "; ".join(undo_errors)
            return CopyOutcome(ok=False, error=error)
    return CopyOutcome(ok=True, digests=digests)


@dataclass(frozen=True, slots=True)
class PruneOutcome:
    pruned: list[TransferEntry]
    ok: bool
    failed_name: str = ""
    error: str = ""


def _prune_phase(
    entries: list[TransferEntry], write_committed: Callable[[list[TransferEntry]], None]
) -> PruneOutcome:
    """Remove each entry's `src`, one at a time — but only ever AFTER
    `write_committed` (a durable ledger write naming it, merged with
    whatever this transaction already committed) has itself succeeded
    (#504 R3-B2: the old code called its "record ownership" callback
    BEFORE removal too, but swallowed the callback's own failure and
    removed the source anyway — a failed record must instead refuse the
    removal it was meant to precede). `write_committed` is called with the
    CUMULATIVE list of entries committed so far each time, so a real
    process death between that write returning and the following
    `_remove()` call still leaves an accurate, durable record — a resumed
    boot's candidates come from live disk state, never this ledger, so
    which side of that gap the crash landed on never matters.

    A whole batch's removals are atomic: if the ledger write OR the
    removal itself fails for any one entry, every entry this call already
    removed (plus the one that just failed, in case its own removal
    partially completed before raising — #504 R3 `partial_final_directory
    _remove`) is reconstructed file-by-file from its own already-verified
    `dest`, and `write_committed([])` reverts the ledger back to exactly
    what it held before this call started."""
    pruned: list[TransferEntry] = []
    failed: TransferEntry | None = None
    error = ""
    for entry in entries:
        try:
            write_committed([*pruned, entry])
        except OSError as e:
            failed, error = entry, f"could not record removal of {entry.name}: {e}"
            break
        try:
            _remove(entry.src)
        except OSError as e:
            failed, error = entry, str(e)
            break
        pruned.append(entry)

    if failed is None:
        return PruneOutcome(pruned=pruned, ok=True)

    restore_errors: list[str] = []
    for e in [*pruned, failed]:
        restore_errors.extend(e.restore_source_from_dest())
    if restore_errors:
        error += "; restore incomplete: " + "; ".join(restore_errors)
    try:
        write_committed([])
    except OSError as e:
        error += f"; could not revert ledger to prior state: {e}"
        _log_event("migration_ledger_revert_failed", error=str(e))
    return PruneOutcome(pruned=[], ok=False, failed_name=failed.name, error=error)


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
            }
        )
    return out


def _find_manifest_by_ts(data_home: Path, ts: str) -> Path | None:
    m = data_home / _ARCHIVE_DIR_NAME / f"v1-archive-{ts}" / _MANIFEST_NAME
    return m if m.is_file() else None


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
    before this command"."""
    root = migration_home() / "restore-v1-snapshots" / f"{time.time():.6f}"
    for name in names:
        target = data_home / name
        if not target.exists():
            continue
        dest = root / name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.copytree(target, dest)
            else:
                shutil.copy2(target, dest)
        except OSError:
            pass
    return CommandSnapshot(root=root, names=tuple(names))


def _revert_to_command_snapshot(data_home: Path, snapshot: CommandSnapshot) -> None:
    """Undo a whole `restore-v1` command by putting every snapshotted name
    back exactly as `_begin_command_snapshot` found it — present names
    copied back, names that were absent before the command removed again.
    Best-effort per name, same as every other undo path in this module: a
    failure here must not compound the original restore failure with a
    second one."""
    for name in snapshot.names:
        current = data_home / name
        try:
            if current.is_dir():
                shutil.rmtree(current, ignore_errors=True)
            elif current.exists():
                current.unlink()
        except OSError:
            pass
        saved = snapshot.root / name
        try:
            if saved.is_dir():
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(saved, current)
            elif saved.is_file():
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, current)
        except OSError:
            pass


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

    def _promoted_member_problems(self) -> list[str]:
        """#504 R3 `promoted_member_inventory`: every file EVER recorded as
        promoted must still be present at its target — `_pending()` alone
        only proves the (by-then-empty) `v2/` root is gone, never that
        nothing promoted from it has since disappeared."""
        path = self._manifest_path()
        if not path.is_file():
            return []
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return [f"promote manifest unreadable: {e}"]
        problems: list[str] = []
        for entry in manifest.get("promoted", []):
            name = entry["name"]
            kind = entry.get("kind", "file")
            if kind == "file":
                if not (self.data_home / name).is_file():
                    problems.append(f"missing: {self.data_home / name}")
                continue
            paths = entry.get("paths")
            if paths is None:
                continue  # pre-#504-fix manifest — no per-file record to check
            for rel in paths:
                if not (self.data_home / name / rel).is_file():
                    problems.append(f"missing: {self.data_home / name / rel}")
        return problems

    def apply(self) -> StepReport:
        if not self._pending():
            self.journal.record(self.step_id, "apply", True, "nothing pending")
            return StepReport(
                self.step_id, "apply", True, "no legacy v2/ root — nothing to promote"
            )

        candidates = self._promote_candidates()
        try:
            existing = _read_committed_entries(self._manifest_path(), "promoted")
        except (OSError, ValueError) as e:
            msg = (
                f"existing promote record at {self._manifest_path()} is unreadable — "
                f"refusing to touch V1 data: {e}"
            )
            self.journal.record(self.step_id, "apply", False, msg)
            return StepReport(self.step_id, "apply", False, msg)

        new_entries: list[TransferEntry] = []
        for src in candidates:
            dest = self.data_home / src.name
            if src.is_dir():
                paths = tuple(p.as_posix() for p in _rel_files(src))
                new_entries.append(TransferEntry(src.name, "dir", src, dest, paths))
            else:
                new_entries.append(TransferEntry(src.name, "file", src, dest))
        new_names = {e.name for e in new_entries}

        def write_committed(committed_now: list[TransferEntry]) -> None:
            merged = [e for e in existing if e["name"] not in new_names]
            merged += [e.to_ledger() for e in committed_now]
            write_json_atomic(
                self._manifest_path(), {"schema": 3, "created_at": time.time(), "promoted": merged}
            )

        copied = _copy_phase(new_entries, self.backups, self.step_id)
        if not copied.ok:
            self.journal.record(self.step_id, "apply", False, copied.error)
            return StepReport(
                self.step_id, "apply", False, f"promote failed, rolled back: {copied.error}"
            )

        prune = _prune_phase(new_entries, write_committed)
        if not prune.ok:
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                f"promote copy-verified {len(new_entries)} item(s) but a source could not be "
                f"removed — every source has been restored, nothing lost, retry apply: {prune.error}",
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
        if not manifest_path.is_file():
            self.journal.record(self.step_id, "rollback", True, "never promoted — nothing to undo")
            return StepReport(
                self.step_id, "rollback", True, "no promote manifest — nothing to undo"
            )
        try:
            existing = _read_committed_entries(manifest_path, "promoted")
        except (OSError, ValueError) as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"could not read manifest: {e}")

        legacy_root = self._legacy_root()
        entries: list[TransferEntry] = []
        prune_map: dict[Path, list[str]] = {}
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
                # exists to restore surgically. Skip rather than guess at
                # moving the whole (possibly-merged) top-level dir back.
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
            self.journal.record(self.step_id, "rollback", True, "nothing left to restore")
            return StepReport(self.step_id, "rollback", True, "nothing left to restore")

        def write_committed(restored_now: list[TransferEntry]) -> None:
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

        copied = _copy_phase(entries, self.backups, self.step_id)
        if not copied.ok:
            self.journal.record(self.step_id, "rollback", False, copied.error)
            return StepReport(self.step_id, "rollback", False, f"restore failed: {copied.error}")

        prune = _prune_phase(entries, write_committed)
        if not prune.ok:
            self.journal.record(self.step_id, "rollback", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "rollback",
                False,
                f"restore copy-verified {len(entries)} item(s) but a source could not be removed "
                f"from its original location — every source has been restored, nothing lost, "
                f"retry rollback: {prune.error}",
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
                continue
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

        ts = f"{time.time():.6f}"
        archive_root = self._archive_base() / f"v1-archive-{ts}"
        manifest_path = archive_root / _MANIFEST_NAME
        candidates = self._archive_candidates() + self._shared_dir_legacy_candidates()

        new_entries: list[TransferEntry] = []
        for src in candidates:
            name = src.relative_to(self.data_home).as_posix()
            dest = archive_root / name
            if src.is_dir():
                paths = tuple(p.as_posix() for p in _rel_files(src))
                new_entries.append(TransferEntry(name, "dir", src, dest, paths))
            else:
                new_entries.append(TransferEntry(name, "file", src, dest))

        copied = _copy_phase(new_entries, self.backups, self.step_id)
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
            except OSError:
                pass
            self.journal.record(self.step_id, "apply", False, copied.error)
            return StepReport(
                self.step_id, "apply", False, f"archive failed, rolled back: {copied.error}"
            )

        def write_committed(committed_now: list[TransferEntry]) -> None:
            archived = [e.to_ledger(copied.digests.get(e.name)) for e in committed_now]
            write_json_atomic(
                manifest_path,
                {
                    "schema": 2,
                    "created_at": time.time(),
                    "archived": archived,
                    "legacy_v2_marker_archived": False,
                    "deleted": [],
                },
            )

        prune = _prune_phase(new_entries, write_committed)
        if not prune.ok:
            self.journal.record(self.step_id, "apply", False, f"cleanup-pending: {prune.error}")
            return StepReport(
                self.step_id,
                "apply",
                False,
                f"archive copy-verified {len(new_entries)} item(s) but a source could not be "
                f"removed — every source has been restored, nothing lost, retry apply: {prune.error}",
                detail={"cleanup_pending": [prune.failed_name]},
            )

        done = prune.pruned
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
                        "archived": [e.to_ledger(copied.digests.get(e.name)) for e in done],
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
                "archived": [e.to_ledger(copied.digests.get(e.name)) for e in done],
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
            except OSError:
                pass

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
        # or corrupt.
        problems = _archive_entry_problems(archive_root, manifest)
        if problems:
            msg = (
                f"archive at {archive_root} is incomplete/corrupt "
                f"({len(problems)} problem(s)) — refusing to restore: {'; '.join(problems[:5])}"
            )
            self.journal.record(self.step_id, "rollback", False, msg)
            return StepReport(self.step_id, "rollback", False, msg, detail={"problems": problems})

        entries: list[TransferEntry] = []
        for entry in manifest.get("archived", []):
            name = entry["name"]
            rel = entry.get("path")
            src = archive_root / rel if rel is not None else Path(entry.get("path", ""))
            entries.append(TransferEntry(name, "file", src, self.data_home / name))

        # Copy-only — the archive itself is never removed by a restore.
        copied = _copy_phase(entries, self.backups, self.step_id)
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
            detail={"archive_root": str(archive_root), "restored": restored},
        )
