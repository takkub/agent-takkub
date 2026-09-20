"""Provider-neutral project memory (#687).

Claude Code's native auto-memory writes to
``<CLAUDE_CONFIG_DIR>/projects/<dir>/memory/`` — a location that is
(a) claude-only (codex/gemini-agy/opencode/kimi/cursor have no such
mechanism) and (b) not redirectable independently of the whole config dir.
Switching the Lead away from claude therefore silently lost every learned
lesson, with no warning (#687).

This module gives the memory one provider-neutral home:

    RUNTIME_DIR/memory/<project>/           (central — what every provider
                                             is pointed at)

and keeps claude's native memory dirs and the central dir in sync
(newest-wins per-file copy, never deletes — the #504 invariant: every file
keeps >=1 copy on every path, so a bad sync can lose nothing). The first
sync of an existing project doubles as the migration of its legacy
native-only files; no boot-migration ladder step is needed.

Naming follows `role_memory.py` (`RUNTIME_DIR/role-memory/<project>/`),
the sibling store whose docstring already reserved this gap ("Lead is
intentionally excluded — it owns the project-wide MEMORY.md instead").
"""

from __future__ import annotations

import logging
import pathlib
import shutil

from .config import RUNTIME_DIR, lead_cwd
from .path_safe import safe_segment

_log = logging.getLogger(__name__)

# claude memory dirs use two naming schemes over time:
#   takkub-project-<ns>            (CLAUDE_CODE_PROJECT_DIR_NAME, >=2.1.234)
#   <encode_path_for_claude(cwd)>  (legacy encoded-cwd)
# Role-suffixed teammate dirs (takkub-project-<ns>-<role>) are deliberately
# NOT synced: #516 F1 split them off precisely so teammate memory stops
# pooling into Lead's project MEMORY.md — the central store is Lead's.
_PROJECT_DIR_PREFIX = "takkub-project-"


def central_dir(project_ns: str) -> pathlib.Path:
    return RUNTIME_DIR / "memory" / safe_segment(project_ns)


def central_memory_md(project_ns: str) -> pathlib.Path:
    return central_dir(project_ns) / "MEMORY.md"


def memory_entry_count(project_ns: str) -> int:
    """Number of memory entry files in the central store (index excluded)."""
    try:
        return sum(1 for p in central_dir(project_ns).glob("*.md") if p.name != "MEMORY.md")
    except OSError:
        return 0


def _native_memory_dirs(project_ns: str, cwd: str | None = None) -> list[pathlib.Path]:
    """claude's native memory dirs for *project_ns*, across both naming
    schemes (and role-suffixed teammate dirs). Only existing dirs returned."""
    dirs: list[pathlib.Path] = []
    try:
        from .user_profile import config_dir_for

        projects_root = pathlib.Path(config_dir_for(project_ns)) / "projects"
    except Exception:
        return dirs
    if not projects_root.is_dir():
        return dirs

    wanted = {f"{_PROJECT_DIR_PREFIX}{project_ns}"}
    try:
        from .token_meter import encode_path_for_claude

        target_cwd = cwd or lead_cwd(project_ns)
        if target_cwd:
            wanted.add(encode_path_for_claude(target_cwd))
    except Exception:
        pass

    for name in wanted:
        mem = projects_root / name / "memory"
        try:
            if mem.is_dir():
                dirs.append(mem)
        except OSError:
            pass
    return dirs


def _sync_pair(src: pathlib.Path, dst: pathlib.Path) -> int:
    """Copy files src→dst where dst is missing or older (newest-wins).
    Never deletes. Returns files copied. `copy2` preserves mtime so a
    re-run is a no-op."""
    copied = 0
    try:
        entries = list(src.iterdir())
    except OSError:
        return 0
    for f in entries:
        if not f.is_file():
            continue
        target = dst / f.name
        try:
            src_m = f.stat().st_mtime
            if target.exists() and target.stat().st_mtime >= src_m - 1e-3:
                continue
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied += 1
        except OSError:
            _log.warning("project-memory sync copy failed: %s -> %s", f, target)
    return copied


def sync(project_ns: str, cwd: str | None = None) -> int:
    """Two-way newest-wins sync between claude's native memory dirs and the
    central store. Cheap (mtime compares on a handful of small .md files);
    called at spawn time. Returns total files copied either direction."""
    if not project_ns:
        return 0
    central = central_dir(project_ns)
    copied = 0
    for native in _native_memory_dirs(project_ns, cwd=cwd):
        copied += _sync_pair(native, central)
        copied += _sync_pair(central, native)
    return copied
