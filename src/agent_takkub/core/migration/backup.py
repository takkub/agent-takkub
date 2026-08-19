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

from ..storage.paths import core_home

_BACKUP_ROOT_NAME = "migration_backups"


class BackupManager:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (core_home() / _BACKUP_ROOT_NAME)

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
            return None
        for slot in slots:
            candidate = slot / name
            if candidate.exists():
                return candidate
        return None
