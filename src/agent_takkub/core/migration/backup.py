"""Copy-never-move backups for migration steps (plan §5.1: "copy-never-move
จนกว่า validate ผ่าน", precedent: `provider_bootstrap.ensure_provider_home`'s
`.partial` + marker + `os.replace`). A backup is taken BEFORE a step mutates
anything so rollback can restore it; the original at *source* is never
deleted here.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..storage.paths import migration_home

_BACKUP_ROOT_NAME = "migration_backups"


class BackupManager:
    def __init__(self, root: Path | None = None) -> None:
        # Deliberately `migration_home()`, NOT `core_home()` — see
        # `migration_home()`'s docstring (#362).
        self._root = root or (migration_home() / _BACKUP_ROOT_NAME)

    @property
    def root(self) -> Path:
        """Where backups are stored — `CoreInternalStoreStep` (#360) needs
        this to exclude its own backup tree from the copy into `v2/system/`."""
        return self._root

    def backup(self, step_id: str, source: Path) -> Path | None:
        """Copy *source* (file or dir) into a timestamped slot under this
        step's backup dir. Returns None (no-op, not an error) when *source*
        doesn't exist yet — a step whose target is brand new has nothing to
        back up."""
        if not source.exists():
            return None
        dest_dir = self._root / step_id / f"{time.time():.6f}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)
        return dest

    def restore(self, backup_path: Path, dest: Path) -> None:
        """Copy the backup back over *dest* (still copy-never-move — the
        backup slot stays intact after a rollback, in case rollback needs
        retrying)."""
        if backup_path.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(backup_path, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, dest)

    def latest_backup(self, step_id: str, name: str) -> Path | None:
        step_dir = self._root / step_id
        if not step_dir.is_dir():
            return None
        try:
            slots = sorted((p for p in step_dir.iterdir() if p.is_dir()), reverse=True)
        except OSError:
            return None  # swallow-ok: read-only listing; caller treats a
            # missing backup the same as one it can't even enumerate.
        for slot in slots:
            candidate = slot / name
            if candidate.exists():
                return candidate
        return None

    def earliest_backup_since(self, step_id: str, name: str, since_ts: float) -> Path | None:
        """Like `latest_backup`, but the OLDEST slot at or after *since_ts*
        instead of the newest overall — #504 round4 `snapshot_revert_middle`:
        a multi-generation `restore-v1` calls `backup()` once per generation
        it touches, so by the time a LATER generation fails mid-walk, the
        newest slot only holds the PREVIOUS generation's already-applied
        state, not the true pre-command preimage. The oldest slot taken
        since the command's own snapshot began (`since_ts` is that
        snapshot's timestamp) is the one captured the moment this command's
        walk FIRST overwrote *name* — i.e. the pre-command state."""
        step_dir = self._root / step_id
        if not step_dir.is_dir():
            return None
        try:
            slots = sorted(p for p in step_dir.iterdir() if p.is_dir())
        except OSError:
            return None  # swallow-ok: same as `latest_backup` above.
        for slot in slots:
            try:
                ts = float(slot.name)
            except ValueError:
                continue  # swallow-ok: not one of this manager's own
                # timestamped slot dirs (foreign/unexpected name) — skip it,
                # never touches anything.
            if ts < since_ts:
                continue
            candidate = slot / name
            if candidate.exists():
                return candidate
        return None
