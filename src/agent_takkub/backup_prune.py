"""`takkub backups list|prune` — what sits in ``DATA_HOME/backups`` and
removing the old safety copies once they are no longer needed.

The V1→V2 migration and later one-time cleanups each leave a full copy here
(a V1 archive alone was 11 GB on the maintainer's machine). Nothing in the
cockpit deletes them on its own — storage V2 never deletes data — so this is
the user-triggered way out, available on every install.

Prune rules: the storage layout must already be fully V2 (migration
finished), an entry must be older than ``MIN_AGE_DAYS``, and
``KEEP`` entries stay (``takkub doctor`` reads the promote manifest).
Removing ``v1-archive-*`` ends ``takkub migrate restore-v1`` (downgrade to
2.0.x) — the listing says so before anything is deleted.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

MIN_AGE_DAYS = 14
KEEP = frozenset({"promote-v2-root-manifest.json"})
WARN_BYTES = 1_000_000_000


@dataclass(frozen=True)
class BackupEntry:
    name: str
    path: Path
    size: int
    age_days: float
    prunable: bool
    reason: str


def backups_dir() -> Path:
    return config.DATA_HOME / "backups"


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _migration_finished() -> bool:
    from .core.storage.layout import layout_state

    return config.DATA_HOME != config.REPO_ROOT and layout_state() == "v2"


def list_backups(now: float | None = None) -> list[BackupEntry]:
    root = backups_dir()
    if not root.is_dir():
        return []
    now = now or time.time()
    finished = _migration_finished()
    entries: list[BackupEntry] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            age = (now - path.stat().st_mtime) / 86_400
        except OSError:
            continue
        if path.name in KEEP:
            prunable, reason = False, "kept (read by takkub doctor)"
        elif not finished:
            prunable, reason = False, "migration not finished — kept"
        elif age < MIN_AGE_DAYS:
            prunable, reason = False, f"newer than {MIN_AGE_DAYS} days — kept"
        else:
            prunable, reason = True, "old safety copy"
        entries.append(BackupEntry(path.name, path, _size(path), age, prunable, reason))
    return entries


def _force_writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def prune(entries: list[BackupEntry]) -> tuple[int, list[str]]:
    """Delete the prunable entries; returns (bytes freed, errors)."""
    freed = 0
    errors: list[str] = []
    for e in entries:
        if not e.prunable:
            continue
        try:
            if e.path.is_dir() and not e.path.is_symlink():
                if sys.version_info >= (3, 12):
                    shutil.rmtree(e.path, onexc=_force_writable)
                else:
                    shutil.rmtree(e.path, onerror=_force_writable)
            else:
                e.path.unlink()
            freed += e.size
        except OSError as exc:
            errors.append(f"{e.name}: {exc}")
    return freed, errors


def _fmt(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def run_cli(cmd: str, *, yes: bool) -> dict:
    entries = list_backups()
    total = sum(e.size for e in entries)
    candidates = [e for e in entries if e.prunable]
    print(f"  {backups_dir()}  ({_fmt(total)} in {len(entries)} entries)")
    for e in entries:
        mark = "🗑" if e.prunable else "·"
        print(f"  {mark} {_fmt(e.size):>10}  {e.age_days:5.0f}d  {e.name}  — {e.reason}")
    if cmd == "list":
        return {"ok": True, "total_bytes": total, "prunable": [e.name for e in candidates]}
    if not candidates:
        print("  nothing to prune")
        return {"ok": True, "freed_bytes": 0}
    if any(e.name.startswith("v1-archive-") for e in candidates):
        print("  ⚠ removing v1-archive-* ends `takkub migrate restore-v1` (downgrade to 2.0.x)")
    if not yes:
        print(f"  dry run — would free {_fmt(sum(e.size for e in candidates))}; re-run with --yes")
        return {"ok": True, "dry_run": True, "prunable": [e.name for e in candidates]}
    freed, errors = prune(candidates)
    print(f"  freed {_fmt(freed)}")
    for err in errors:
        print(f"  ⚠ {err}")
    return {"ok": not errors, "freed_bytes": freed, "errors": errors}
