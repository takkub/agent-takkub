"""Disk-usage reporting + pruning for the cockpit's own DATA_HOME.

``takkub disk`` reports where DATA_HOME's bytes went, bucketed into safety
levels (safe / review / never) so the user can decide what to reclaim.
``takkub prune`` deletes only what the user explicitly opts into — always
dry-run unless ``--yes`` is passed, and refuses anything tagged ``never``
regardless of what the caller asks for.

Pure module: stdlib + config + worktree_manager + orchestrator_text +
shared_dev_tools (all leaf-safe already — see the worktree-manager-leaf /
orchestrator-text-layer import-linter contracts). Must NOT import
orchestrator/cli_server/UI so that ``cli.py`` (which imports this module
directly, no orchestrator socket needed) stays clear of the
cli-ipc-boundary contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import graft_store
from .config import DATA_HOME, RUNTIME_DIR, default_claude_config_dir, load_projects
from .orchestrator_text import prune_old_transcripts
from .shared_dev_tools import prune_old_browser_profiles
from .worktree_manager import WorktreeManager, sweep_link_points
from .worktree_manager import dir_stats as _dir_stats

SAFE = "safe"
REVIEW = "review"
NEVER = "never"

# Categories `takkub prune --category` accepts. Anything else is either an
# unknown key or a known-but-never-prunable one (see _NEVER_CATEGORIES) —
# both are refused, never silently ignored.
VALID_CATEGORIES: tuple[str, ...] = (
    "browser-profiles",
    "transcripts",
    "exports",
    "orphan-worktrees",
    "orphan-worktrees-review",
    "shell-snapshots",
    "partial",
    "chat-history",
    "node-modules",
    "graft-graphs",
)

# Categories the report is aware of but which `prune` must always refuse,
# with a human reason surfaced back to the caller.
_NEVER_CATEGORIES: dict[str, str] = {
    "venv": "Python virtualenv — cockpit ใช้รันอยู่ ลบแล้วคุมไม่ได้",
    "claude-config-other": "ไฟล์/โฟลเดอร์อื่นใน claude-config (plugins, credentials, ...) "
    "— ไม่ทราบผลกระทบ ปลอดภัยไว้ก่อน",
    "tasks": "task ledger ของโปรเจค — เล็ก ไม่คุ้มเสี่ยง",
    "live-worktrees": "worktree ที่ยังลงทะเบียนอยู่ (อาจมีงานค้าง/pane กำลังใช้) "
    "— ใช้ `takkub worktree clean`/`merge` แทน",
}

_DEFAULT_TRANSCRIPT_RETENTION_DAYS = 7  # mirrors orchestrator_text._TRANSCRIPT_RETENTION_DAYS
_DEFAULT_BROWSER_PROFILE_RETENTION_DAYS = (
    14  # mirrors shared_dev_tools._BROWSER_PROFILE_RETENTION_DAYS
)
_DEFAULT_EXPORTS_RETENTION_DAYS = 14
_DEFAULT_SHELL_SNAPSHOT_RETENTION_DAYS = 7
_DEFAULT_CHAT_HISTORY_RETENTION_DAYS = 30
_PARTIAL_MIN_AGE_SECONDS = 600  # don't touch a partial clone that might still be running


# ── low-level filesystem helpers ────────────────────────────────────────────


def _dir_max_mtime(path: Path) -> float:
    """Newest mtime of any file under *path* (or of *path* itself). 0.0 if empty/missing."""
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        pass
    if not path.is_dir():
        return newest
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for f in files:
                try:
                    newest = max(newest, (Path(root) / f).stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def _assert_under(path: Path, base: Path) -> Path:
    """Resolve *path* and raise ValueError unless it lives under *base*.

    Every deletion target must pass this — the last line of defense against
    a crafted/absolute category argument escaping DATA_HOME.
    """
    base_r = base.resolve()
    path_r = path.resolve()
    if path_r != base_r and base_r not in path_r.parents:
        raise ValueError(f"path ไม่อยู่ใต้ DATA_HOME ({base_r}): {path_r}")
    return path_r


def _onerror_chmod_retry(func, path, _exc_info) -> None:
    """shutil.rmtree onerror hook: clear read-only then retry once."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    try:
        func(path)
    except OSError:
        pass


def _win_long_path(path: Path) -> str:
    """Extended-length ``\\\\?\\`` form so rmtree survives >260-char paths."""
    s = str(path)
    if not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def robust_rmtree(path: Path) -> tuple[bool, list[str]]:
    r"""Best-effort recursive delete that survives Windows long paths + read-only files.

    node_modules trees are exactly the shape that breaks a plain
    ``shutil.rmtree`` on Windows: deeply nested (easily >260 chars) and full
    of read-only files (npm/git-installed packages). On win32 this deletes
    through the ``\\?\`` extended-length prefix and retries any read-only
    failure via chmod. Returns ``(fully_removed, leftover_paths)`` — a
    partial delete is reported, never silently swallowed as success.
    """
    path = path.resolve()
    if not path.exists():
        return True, []
    target = _win_long_path(path) if sys.platform == "win32" else str(path)
    try:
        shutil.rmtree(target, onerror=_onerror_chmod_retry)
    except OSError:
        pass
    if path.exists():
        leftover: list[str] = []
        try:
            for root, dirs, files in os.walk(path, onerror=lambda _e: None):
                for f in files:
                    leftover.append(str(Path(root) / f))
                if not dirs and not files:
                    leftover.append(root)
        except OSError:
            leftover.append(str(path))
        return False, leftover[:50]
    return True, []


# ── worktree classification ─────────────────────────────────────────────────


def find_worktree_dirs(data_home: Path) -> list[Path]:
    """Every per-pane checkout dir on disk under ``DATA_HOME/worktrees/*/*``."""
    root = data_home / "worktrees"
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for wt_dir in project_dir.iterdir():
                if wt_dir.is_dir():
                    out.append(wt_dir)
    except OSError:
        pass
    return out


def _pane_active_paths(runtime_dir: Path | None = None) -> set[str]:
    """Best-effort cwd set from the last session snapshot.

    NOT authoritative (file can be stale, or the cockpit closed since) — used
    only to annotate a report entry with a "a pane may still be using this"
    hint. It never changes safe/review/never classification; the flag gate
    (``--include-live``) is what actually blocks deletion of a registered
    worktree's node_modules. *runtime_dir* defaults to the real cockpit
    RUNTIME_DIR; callers scanning a caller-supplied ``data_home`` (tests, a
    non-default instance) pass ``home / "runtime"`` instead so this never
    reads a different instance's snapshot.
    """
    runtime_dir = runtime_dir or RUNTIME_DIR
    snap_file = runtime_dir / "last-session.json"
    if not snap_file.is_file():
        return set()
    try:
        data = json.loads(snap_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    paths: set[str] = set()
    for entries in (data.get("projects") or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            cwd = (e or {}).get("cwd")
            if cwd:
                try:
                    paths.add(str(Path(cwd).resolve()))
                except OSError:
                    continue
    return paths


def classify_worktree(
    wt_dir: Path, mgr: WorktreeManager, *, runtime_dir: Path | None = None
) -> dict:
    """Describe one on-disk worktree checkout dir.

    Orphan = no longer known to git as a live worktree: either the ``.git``
    pointer file is gone, its source repo can't be resolved anymore, or
    ``git worktree list`` no longer lists this exact path (e.g. it was
    pruned already). Everything else is "registered" (still live per git)
    and carries dirty/ahead flags from the same logic ``takkub worktree
    list`` uses.

    Being orphaned only means "git no longer tracks it" — NOT "safe to
    delete" (#132). When the checkout still responds to git directly (the
    ``git worktree list`` mismatch case), this also fills in best-effort
    ``dirty``/``branch``/``ahead`` so callers can tell a genuinely inert
    leftover from one whose branch still carries unmerged commits. Those two
    stay ``None``/unset when git can't be read at all (no ``.git`` pointer,
    or its source repo can't be resolved).
    """
    row: dict = {"path": str(wt_dir)}
    git_file = wt_dir / ".git"
    if not git_file.exists():
        row.update(orphan=True, registered=False, dirty=None, branch=None, ahead=None)
        return row
    root = mgr.git_root(str(wt_dir))
    if root is None:
        row.update(orphan=True, registered=False, dirty=None, branch=None, ahead=None)
        return row
    match = next(
        (r for r in mgr.list_isolated(root) if Path(r["path"]).resolve() == wt_dir.resolve()),
        None,
    )
    if match is None:
        branch = mgr.current_branch(str(wt_dir))
        dirty = mgr.is_dirty_at(str(wt_dir))
        ahead = mgr.commits_ahead(root, branch) if branch else 0
        row.update(orphan=True, registered=False, dirty=dirty, branch=branch, ahead=ahead)
        return row
    row.update(
        orphan=False,
        registered=True,
        branch=match["branch"],
        dirty=match["dirty"],
        ahead=match["ahead"],
        pane_active=str(wt_dir.resolve()) in _pane_active_paths(runtime_dir),
    )
    return row


def _worktree_has_only_regenerable_content(wt_dir: Path) -> bool:
    """True when every file under *wt_dir* lives inside a ``node_modules``
    directory (or there is no file at all).

    This is the ONLY orphan shape an unattended sweep may delete without
    risking unrecoverable source loss (#132): an empty checkout, or one
    whose sole content is npm-reinstallable. Anything else — a single stray
    source file is enough — fails this check and must go through explicit
    ``review``-level prune instead. The worktree's own ``.git`` pointer file
    is ignored (it is metadata, not content the user could lose).
    """
    try:
        for root, _dirs, files in os.walk(wt_dir, onerror=lambda _e: None):
            rel_parts = Path(root).relative_to(wt_dir).parts
            if "node_modules" in rel_parts:
                continue  # inside a node_modules subtree — regenerable
            for f in files:
                if f != ".git":
                    return False
    except OSError:
        return False
    return True


def _orphan_prune_bucket(wt_dir: Path, info: dict) -> str:
    """SAFE or REVIEW bucket for an orphan row (#132).

    SAFE (auto-prunable, including by the unattended boot sweep) requires
    ALL of: no uncommitted changes, no branch commits still unmerged, and no
    content beyond an empty dir / node_modules. Any one of those failing
    demotes the row to REVIEW — a human must opt in via ``takkub prune
    --level review --category orphan-worktrees-review --yes``.
    """
    if info.get("dirty"):
        return REVIEW
    if (info.get("ahead") or 0) > 0:
        return REVIEW
    if not _worktree_has_only_regenerable_content(wt_dir):
        return REVIEW
    return SAFE


# ── node_modules deep scan ───────────────────────────────────────────────────


def find_node_modules(root: Path) -> list[Path]:
    """Every ``node_modules`` dir under *root*, at any depth.

    Does not descend into a found ``node_modules`` (its own nested
    node_modules, if any, is still counted — it's part of the same subtree a
    single rmtree removes as one unit; walking into it separately would only
    double-count).
    """
    found: list[Path] = []
    if not root.is_dir():
        return found
    try:
        for dirpath, dirnames, _files in os.walk(root, onerror=lambda _e: None):
            if "node_modules" in dirnames:
                found.append(Path(dirpath) / "node_modules")
                dirnames.remove("node_modules")
    except OSError:
        pass
    return found


def scan_node_modules(data_home: Path, mgr: WorktreeManager | None = None) -> dict:
    """Aggregate node_modules usage across every worktree, split orphan vs. live."""
    mgr = mgr or WorktreeManager()
    entries: list[dict] = []
    orphan_bytes = orphan_count = 0
    live_bytes = live_count = 0
    for wt_dir in find_worktree_dirs(data_home):
        info = classify_worktree(wt_dir, mgr, runtime_dir=data_home / "runtime")
        for nm in find_node_modules(wt_dir):
            size, count = _dir_stats(nm)
            entry = {
                "path": str(nm),
                "worktree": str(wt_dir),
                "orphan": info["orphan"],
                "pane_active": bool(info.get("pane_active")),
                "size_bytes": size,
                "file_count": count,
            }
            entries.append(entry)
            if info["orphan"]:
                orphan_bytes += size
                orphan_count += 1
            else:
                live_bytes += size
                live_count += 1
    return {
        "entries": entries,
        "orphan_bytes": orphan_bytes,
        "orphan_count": orphan_count,
        "live_bytes": live_bytes,
        "live_count": live_count,
    }


# ── graft graph store (#146 follow-up) ───────────────────────────────────────


# H1(c) — a live graft-graphs store had no reclaim path at all and no
# visibility: `_prune_graft_graphs` only ever touched orphans, so a store
# that grew unbounded (measured: 507 MB / 4954 files for one project, 72%
# of it gitignored bulk graft's own walker doesn't recognize — see
# graft_autobuild.py's module docstring) sat there forever with nothing in
# `takkub disk`'s output calling it out. Chosen to match `node-modules`'s
# `--include-live` precedent below (`_prune_node_modules`) rather than
# invent a second reclaim UX.
_GRAFT_STORE_WARN_BYTES = 200 * 1024 * 1024

# H2 migration (2026-08-05 cross-OS audit) — pre-fix `graft_store.py` keyed
# both the instance dir and each per-target store dir with a FULL 64-hex-char
# SHA-256 digest; H2 truncated both to 16 hex chars to reclaim Windows
# MAX_PATH budget. A 64-char name can therefore never be produced by current
# code, so it unambiguously identifies pre-fix leftovers — unlike a same-
# length (16-char) sibling instance dir, which might be a second cockpit
# instance that is genuinely still live and must never be touched here.
_LEGACY_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _legacy_orphan_entries() -> list[dict]:
    """Pre-H2 store dirs left behind by the 64-hex-char key format — see
    `_LEGACY_KEY_RE`'s comment. `graph_store.GRAFT_STORE_ROOT` is now
    16-char-keyed, so these sit as siblings one level up
    (`GRAFT_STORE_ROOT.parent`) and were previously invisible to
    `scan_graft_graphs` entirely (it only ever iterated inside the CURRENT
    instance's root) — silently unreclaimable garbage otherwise, exactly
    what H2 requires a migration path NOT leave behind."""
    parent = graft_store.GRAFT_STORE_ROOT.parent
    entries: list[dict] = []
    try:
        instance_dirs = [p for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return entries
    for inst_dir in instance_dirs:
        if not _LEGACY_KEY_RE.match(inst_dir.name):
            continue  # current-format (16-char) — may be a live sibling instance, leave alone
        try:
            target_dirs = [p for p in inst_dir.iterdir() if p.is_dir()]
        except OSError:
            target_dirs = []
        if not target_dirs:
            size, count = _dir_stats(inst_dir)
            entries.append(
                {
                    "path": str(inst_dir),
                    "source": None,
                    "orphan": True,
                    "size_bytes": size,
                    "file_count": count,
                }
            )
            continue
        for store in target_dirs:
            size, count = _dir_stats(store)
            entries.append(
                {
                    "path": str(store),
                    "source": graft_store.read_store_manifest(store),
                    "orphan": True,
                    "size_bytes": size,
                    "file_count": count,
                }
            )
    return entries


def _configured_graph_keys() -> set[str]:
    """`graph_key(...)` for every path currently configured across every
    project in projects.json, regardless of whether that path still exists
    on disk (a transient unmount/offline drive must not reclassify a store
    as orphan). Used to tell a live graft-graphs/ store apart from one whose
    source project/path was removed."""
    keys: set[str] = set()
    try:
        projects = load_projects().get("projects") or {}
    except Exception:
        return keys
    for proj in projects.values():
        if not isinstance(proj, dict):
            continue
        paths = proj.get("paths")
        if not isinstance(paths, dict):
            continue
        for raw in paths.values():
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except OSError:
                continue
            keys.add(graft_store.graph_key(resolved))
    return keys


def scan_graft_graphs(data_home: Path) -> dict:
    """Aggregate `graft-graphs/` usage (external code-graph store, #146
    follow-up), split orphan vs. live by comparing each store dir's own
    name (its `graph_key`) against every path currently configured in
    projects.json. A store whose source project/path was removed (or
    renamed, which mints a different key) shows up as orphan even though
    its bytes are still sitting on disk.

    Each entry's size also folds in its paired staging mirror under
    `graft_store.GRAFT_STAGING_ROOT` (same `graph_key`, H1 follow-up,
    2026-08-06 — `graft_autobuild.staging_dir_for` persists a git-filtered
    copy of the source tree alongside the graph itself now) — one combined
    number per project rather than a second category the user has to
    reason about separately. A store with no paired staging dir (pre-H1-
    follow-up leftover, or a build that failed before staging synced)
    contributes 0 extra bytes, not an error.

    *data_home* is accepted (and left unused) only to keep this function's
    signature uniform with its `scan_*`/`_prune_*` siblings in this module —
    the store root is never DATA_HOME-relative (see `graft_store.py`'s
    module docstring), so the real root always comes from
    `graft_store.GRAFT_STORE_ROOT`.
    """
    root = graft_store.GRAFT_STORE_ROOT
    entries: list[dict] = []
    orphan_bytes = orphan_count = 0
    live_bytes = live_count = 0
    oversized_live: list[dict] = []

    # H2 migration: pre-fix 64-hex-char-keyed leftovers sit one level up
    # from `root` regardless of whether `root` (the CURRENT instance's
    # store) exists yet — check independently of the `root.is_dir()` guard
    # below, or a fresh reinstall with old leftovers but no new build yet
    # would never see them either.
    for legacy in _legacy_orphan_entries():
        entries.append(legacy)
        orphan_bytes += legacy["size_bytes"]
        orphan_count += 1

    if not root.is_dir():
        return {
            "entries": entries,
            "orphan_bytes": orphan_bytes,
            "orphan_count": orphan_count,
            "live_bytes": live_bytes,
            "live_count": live_count,
            "oversized_live": oversized_live,
        }
    configured_keys = _configured_graph_keys()
    try:
        store_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        store_dirs = []
    for store in store_dirs:
        size, count = _dir_stats(store)
        staging = graft_store.GRAFT_STAGING_ROOT / store.name
        if staging.is_dir():
            stage_size, stage_count = _dir_stats(staging)
            size += stage_size
            count += stage_count
        is_orphan = store.name not in configured_keys
        entries.append(
            {
                "path": str(store),
                "source": graft_store.read_store_manifest(store),
                "orphan": is_orphan,
                "size_bytes": size,
                "file_count": count,
            }
        )
        if is_orphan:
            orphan_bytes += size
            orphan_count += 1
        else:
            live_bytes += size
            live_count += 1
            if size > _GRAFT_STORE_WARN_BYTES:
                oversized_live.append({"path": str(store), "size_bytes": size})
    return {
        "entries": entries,
        "orphan_bytes": orphan_bytes,
        "orphan_count": orphan_count,
        "live_bytes": live_bytes,
        "live_count": live_count,
        "oversized_live": oversized_live,
    }


# ── category report ──────────────────────────────────────────────────────────


@dataclass
class CategoryUsage:
    key: str
    label: str
    level: str
    size_bytes: int = 0
    file_count: int = 0
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "level": self.level,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "detail": self.detail,
        }


def _claude_config_categories(data_home: Path) -> list[CategoryUsage]:
    """claude-config/ split into projects (chat history) / shell-snapshots /
    everything else (unknown → never, conservative by design)."""
    cfg_dir = default_claude_config_dir()
    out: list[CategoryUsage] = []
    if not cfg_dir.is_dir():
        return out
    projects_dir = cfg_dir / "projects"
    size, count = _dir_stats(projects_dir)
    out.append(
        CategoryUsage(
            "chat-history",
            f"{cfg_dir.name}/projects (ประวัติแชต JSONL — เสีย /resume + token badge)",
            REVIEW,
            size,
            count,
        )
    )
    snap_dir = cfg_dir / "shell-snapshots"
    size, count = _dir_stats(snap_dir)
    out.append(
        CategoryUsage(
            "shell-snapshots",
            f"{cfg_dir.name}/shell-snapshots (cache คำสั่ง shell — สร้างใหม่ได้)",
            SAFE,
            size,
            count,
        )
    )
    other_bytes = 0
    other_count = 0
    try:
        for child in cfg_dir.iterdir():
            if child.name in ("projects", "shell-snapshots"):
                continue
            s, c = (
                _dir_stats(child)
                if child.is_dir()
                else (child.stat().st_size, 1)
                if child.is_file()
                else (0, 0)
            )
            other_bytes += s
            other_count += c
    except OSError:
        pass
    out.append(
        CategoryUsage(
            "claude-config-other",
            f"{cfg_dir.name}/* อื่นๆ (plugins, credentials, settings, ...)",
            NEVER,
            other_bytes,
            other_count,
        )
    )
    return out


def _partial_dir() -> Path:
    cfg_dir = default_claude_config_dir()
    return cfg_dir.with_name(cfg_dir.name + ".partial")


def _worktree_categories(data_home: Path, mgr: WorktreeManager) -> dict:
    """Per-worktree rows (orphan + registered) — used by both the `disk`
    report and `prune`'s orphan-worktrees categories.

    Every orphan row also carries ``prune_bucket`` (``safe``/``review``,
    #132) so callers can split "nothing to lose" leftovers from ones that
    still hold source/uncommitted/unmerged content without recomputing it.
    """
    orphan_rows: list[dict] = []
    registered_rows: list[dict] = []
    for wt_dir in find_worktree_dirs(data_home):
        info = classify_worktree(wt_dir, mgr, runtime_dir=data_home / "runtime")
        size, count = _dir_stats(wt_dir)
        info["size_bytes"] = size
        info["file_count"] = count
        if info["orphan"]:
            info["prune_bucket"] = _orphan_prune_bucket(wt_dir, info)
            orphan_rows.append(info)
        else:
            registered_rows.append(info)
    return {"orphan": orphan_rows, "registered": registered_rows}


def runtime_layout_triage(data_home: Path | None = None) -> dict:
    """Read-only classification of `runtime/`'s direct children into
    permanent-state vs cache/junk (#309 Phase 8b, audit D6: "runtime/
    ปนกัน 3 ชนิดข้อมูล"), reusing `core.migration.steps_v1`'s own
    classification tables so this and `takkub migrate`'s step 7 never
    drift apart. Awareness only — never deletes or moves anything; that
    stays `takkub prune`'s job (unchanged, no new category added here).
    """
    from .core.migration.steps_v1 import RUNTIME_CACHE_ENTRIES, RUNTIME_STATE_TARGETS

    home = (data_home or DATA_HOME).resolve()
    runtime_dir = home / "runtime"
    if not runtime_dir.is_dir():
        return {"state_dirs": {}, "cache_dirs": {}, "unclassified_dirs": {}}

    known = set(RUNTIME_STATE_TARGETS) | set(RUNTIME_CACHE_ENTRIES)
    state_dirs: dict[str, dict] = {}
    cache_dirs: dict[str, dict] = {}
    unclassified: dict[str, dict] = {}
    try:
        children = [p for p in runtime_dir.iterdir() if p.is_dir()]
    except OSError:
        children = []
    for child in children:
        size, count = _dir_stats(child)
        entry = {"size_bytes": size, "file_count": count}
        if child.name in RUNTIME_STATE_TARGETS:
            state_dirs[child.name] = entry
        elif child.name in RUNTIME_CACHE_ENTRIES:
            cache_dirs[child.name] = entry
        elif child.name not in known:
            unclassified[child.name] = entry
    return {"state_dirs": state_dirs, "cache_dirs": cache_dirs, "unclassified_dirs": unclassified}


def disk_report(data_home: Path | None = None) -> dict:
    """Full categorized disk-usage report for `takkub disk`."""
    home = (data_home or DATA_HOME).resolve()
    categories: list[CategoryUsage] = []

    venv_dir = home / "venv"
    size, count = _dir_stats(venv_dir)
    categories.append(
        CategoryUsage("venv", "venv/ (Python virtualenv — ห้ามแตะ)", NEVER, size, count)
    )

    categories.extend(_claude_config_categories(home))

    partial = _partial_dir()
    size, count = _dir_stats(partial)
    categories.append(
        CategoryUsage("partial", f"{partial.name} (ซากคัดลอก profile ค้าง)", SAFE, size, count)
    )

    runtime_dir = home / "runtime"

    browser_dir = runtime_dir / "browser-profiles"
    size, count = _dir_stats(browser_dir)
    categories.append(
        CategoryUsage(
            "browser-profiles",
            "runtime/browser-profiles (Chromium profile ต่อ shard)",
            SAFE,
            size,
            count,
        )
    )

    sessions_dir = runtime_dir / "sessions"
    t_size = t_count = 0
    if sessions_dir.is_dir():
        try:
            for p in sessions_dir.rglob("*.transcript.log"):
                try:
                    t_size += p.stat().st_size
                    t_count += 1
                except OSError:
                    continue
        except OSError:
            pass
    categories.append(
        CategoryUsage(
            "transcripts",
            "runtime/sessions/*.transcript.log (PTY transcript)",
            SAFE,
            t_size,
            t_count,
        )
    )

    exports_dir = runtime_dir / "exports"
    size, count = _dir_stats(exports_dir)
    categories.append(
        CategoryUsage("exports", "runtime/exports (screenshot/artifact ต่อวัน)", REVIEW, size, count)
    )

    tasks_dir = runtime_dir / "tasks"
    size, count = _dir_stats(tasks_dir)
    categories.append(CategoryUsage("tasks", "runtime/tasks (task ledger)", NEVER, size, count))

    mgr = WorktreeManager()
    wt = _worktree_categories(home, mgr)
    orphan_safe = [r for r in wt["orphan"] if r.get("prune_bucket") == SAFE]
    orphan_review = [r for r in wt["orphan"] if r.get("prune_bucket") == REVIEW]
    orphan_bytes = sum(r["size_bytes"] for r in orphan_safe)
    orphan_count = len(orphan_safe)
    categories.append(
        CategoryUsage(
            "orphan-worktrees",
            "worktrees/* ที่ไม่ลงทะเบียนใน git worktree list แล้ว (crashed/leftover pane) "
            "และว่างเปล่า/มีแต่ node_modules — ลบอัตโนมัติได้ (#132)",
            SAFE,
            orphan_bytes,
            orphan_count,
        )
    )
    review_bytes = sum(r["size_bytes"] for r in orphan_review)
    review_count = len(orphan_review)
    categories.append(
        CategoryUsage(
            "orphan-worktrees-review",
            "worktrees/* ที่ไม่ลงทะเบียนแล้วแต่ยังมีไฟล์ source/uncommitted/commit ค้าง "
            "— ต้อง review เอง (`takkub prune --level review "
            "--category orphan-worktrees-review --yes`, #132)",
            REVIEW,
            review_bytes,
            review_count,
        )
    )
    live_bytes = sum(r["size_bytes"] for r in wt["registered"])
    live_count = len(wt["registered"])
    categories.append(
        CategoryUsage(
            "live-worktrees",
            "worktrees/* ที่ยังลงทะเบียนอยู่ (ใช้ takkub worktree list/clean/merge)",
            NEVER,
            live_bytes,
            live_count,
        )
    )

    nm = scan_node_modules(home, mgr)
    categories.append(
        CategoryUsage(
            "node-modules",
            "node_modules ทุกจุดใต้ worktrees/ (orphan ลบได้ปลอดภัย, live ต้อง --include-live)",
            SAFE,
            nm["orphan_bytes"] + nm["live_bytes"],
            nm["orphan_count"] + nm["live_count"],
            detail=f"orphan {nm['orphan_bytes']} bytes ({nm['orphan_count']}) · "
            f"live {nm['live_bytes']} bytes ({nm['live_count']}, ต้อง --include-live)",
        )
    )

    gg = scan_graft_graphs(home)
    gg_detail = (
        f"orphan {gg['orphan_bytes']} bytes ({gg['orphan_count']}) · "
        f"live {gg['live_bytes']} bytes ({gg['live_count']}, ต้อง --include-live)"
    )
    if gg["oversized_live"]:
        biggest = max(gg["oversized_live"], key=lambda e: e["size_bytes"])
        gg_detail += (
            f" · ⚠ {len(gg['oversized_live'])} store เกิน {_GRAFT_STORE_WARN_BYTES // (1024 * 1024)}MB "
            f"(ใหญ่สุด {biggest['size_bytes']} bytes ที่ {biggest['path']}) — "
            "graft ไม่รู้จัก .gitignore เอง มักมาจาก dir ที่ไม่ใช่ source จริง (H1)"
        )
    categories.append(
        CategoryUsage(
            "graft-graphs",
            "graft-graphs/* (external code-graph store ต่อโปรเจค, #146 — orphan = path "
            "ไม่ได้ configure ใน projects.json แล้ว, ลบอัตโนมัติได้; live ต้อง --include-live)",
            SAFE,
            gg["orphan_bytes"] + gg["live_bytes"],
            gg["orphan_count"] + gg["live_count"],
            detail=gg_detail,
        )
    )

    total = sum(c.size_bytes for c in categories)
    # L2 (2026-08-05 cross-OS audit): `graft-graphs/` bytes get folded into
    # `total_bytes` above like every other category, but they don't
    # necessarily LIVE under `home` — `graft_store.py`'s module docstring
    # documents the one case where they don't (DATA_HOME == REPO_ROOT, a dev
    # checkout, falls back to `Path.home()/".agent-takkub"` instead, see
    # `_graft_store_base`). Reporting only `"data_home"` would silently
    # present a total that spans two different roots under one label.
    # Surface the graft store's REAL root explicitly so a caller (or a human
    # reading `--json` output) can tell the two apart instead of assuming
    # every byte counted here sits under `data_home`.
    graft_root = str(graft_store.GRAFT_STORE_ROOT.parent)
    return {
        "data_home": str(home),
        "graft_store_root": graft_root,
        # L8 (2026-08-06 re-review): a raw `startswith` reads a sibling like
        # `<home>-old` as "inside" `home` — `Path.is_relative_to` compares
        # actual path parts instead of a string prefix (and, on Windows,
        # pathlib's own case-insensitive comparison already covers what the
        # old `os.name == "nt"` branch hand-rolled).
        "graft_store_root_outside_data_home": not Path(graft_root).is_relative_to(home),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_bytes": total,
        "categories": [c.as_dict() for c in categories],
        "worktrees": wt,
        "node_modules": nm,
        "runtime_triage": runtime_layout_triage(home),
    }


# ── prune ─────────────────────────────────────────────────────────────────


_LEVEL_RANK = {SAFE: 0, REVIEW: 1, NEVER: 2}


@dataclass
class PruneOutcome:
    category: str
    level: str
    dry_run: bool
    targets: list[str] = field(default_factory=list)
    would_free_bytes: int = 0
    removed_count: int = 0
    freed_bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "level": self.level,
            "dry_run": self.dry_run,
            "targets": list(self.targets),
            "target_count": len(self.targets),
            "would_free_bytes": self.would_free_bytes,
            "removed_count": self.removed_count,
            "freed_bytes": self.freed_bytes,
            "skipped": self.skipped,
            "errors": self.errors,
            "incomplete": self.incomplete,
        }


def _category_level(category: str) -> str:
    if category in _NEVER_CATEGORIES:
        return NEVER
    levels = {
        "browser-profiles": SAFE,
        "transcripts": SAFE,
        "partial": SAFE,
        "orphan-worktrees": SAFE,
        "orphan-worktrees-review": REVIEW,  # has source/uncommitted/unmerged content (#132)
        "node-modules": SAFE,  # orphan subset is safe; live subset is gated by --include-live
        "shell-snapshots": SAFE,
        "exports": REVIEW,
        "chat-history": REVIEW,
        "graft-graphs": SAFE,  # orphan subset is safe; live subset is gated by --include-live (H1c)
    }
    return levels.get(category, NEVER)


def _rmtree_target(path: Path, outcome: PruneOutcome, size: int) -> None:
    ok, leftover = robust_rmtree(path)
    if ok:
        outcome.removed_count += 1
        outcome.freed_bytes += size
    else:
        outcome.incomplete.append(str(path))
        outcome.errors.append(
            f"{path}: ลบไม่ครบ ({len(leftover)} รายการเหลือ) — อาจเป็น path ยาว/ไฟล์ล็อก"
        )


def _prune_browser_profiles(home: Path, older_than_days: int | None, dry_run: bool) -> PruneOutcome:
    days = (
        older_than_days if older_than_days is not None else _DEFAULT_BROWSER_PROFILE_RETENTION_DAYS
    )
    root = home / "runtime" / "browser-profiles"
    outcome = PruneOutcome("browser-profiles", SAFE, dry_run)
    if not root.is_dir():
        return outcome
    cutoff = time.time() - days * 86_400
    targets_with_size: list[tuple[Path, int]] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        size, _c = _dir_stats(p)
        targets_with_size.append((p, size))
        outcome.targets.append(str(p))
        outcome.would_free_bytes += size
    if not dry_run and outcome.targets:
        if home.resolve() == DATA_HOME.resolve():
            # Real instance: reuse the production pruner as-is — it always
            # targets the true cockpit RUNTIME_DIR directly (see
            # shared_dev_tools.prune_old_browser_profiles), which is exactly
            # `root` above when `home` hasn't been overridden.
            removed = prune_old_browser_profiles(max_age_days=days)
            outcome.removed_count = removed
            outcome.freed_bytes = outcome.would_free_bytes
        else:
            # `home` was overridden (tests / a non-default instance): the
            # reused function can't be redirected, so delete exactly the
            # already-computed targets ourselves instead of ever touching
            # the real DATA_HOME.
            for p, size in targets_with_size:
                _rmtree_target(_assert_under(p, home), outcome, size)
    return outcome


def _prune_transcripts(home: Path, older_than_days: int | None, dry_run: bool) -> PruneOutcome:
    days = older_than_days if older_than_days is not None else _DEFAULT_TRANSCRIPT_RETENTION_DAYS
    sessions = home / "runtime" / "sessions"
    outcome = PruneOutcome("transcripts", SAFE, dry_run)
    if not sessions.is_dir():
        return outcome
    cutoff = time.time() - days * 86_400
    targets_with_size: list[tuple[Path, int]] = []
    try:
        for p in sessions.rglob("*.transcript.log"):
            try:
                st = p.stat()
                if st.st_mtime < cutoff:
                    targets_with_size.append((p, st.st_size))
                    outcome.targets.append(str(p))
                    outcome.would_free_bytes += st.st_size
            except OSError:
                continue
    except OSError:
        pass
    if not dry_run and outcome.targets:
        if home.resolve() == DATA_HOME.resolve():
            removed = prune_old_transcripts(max_age_days=days)
            outcome.removed_count = removed
            outcome.freed_bytes = outcome.would_free_bytes
        else:
            for p, size in targets_with_size:
                p = _assert_under(p, home)
                try:
                    p.unlink()
                    outcome.removed_count += 1
                    outcome.freed_bytes += size
                except OSError as exc:
                    outcome.errors.append(f"{p}: {exc}")
    return outcome


def _prune_exports(home: Path, older_than_days: int | None, dry_run: bool) -> PruneOutcome:
    days = older_than_days if older_than_days is not None else _DEFAULT_EXPORTS_RETENTION_DAYS
    root = home / "runtime" / "exports"
    outcome = PruneOutcome("exports", REVIEW, dry_run)
    if not root.is_dir():
        return outcome
    cutoff = time.time() - days * 86_400
    targets_with_size: list[tuple[Path, int]] = []
    try:
        for date_dir in root.iterdir():
            if not date_dir.is_dir():
                continue
            if _dir_max_mtime(date_dir) >= cutoff:
                continue
            size, _c = _dir_stats(date_dir)
            targets_with_size.append((date_dir, size))
            outcome.targets.append(str(date_dir))
            outcome.would_free_bytes += size
    except OSError:
        pass
    if not dry_run:
        for path, size in targets_with_size:
            _rmtree_target(_assert_under(path, home), outcome, size)
    return outcome


def _prune_shell_snapshots(home: Path, older_than_days: int | None, dry_run: bool) -> PruneOutcome:
    days = (
        older_than_days if older_than_days is not None else _DEFAULT_SHELL_SNAPSHOT_RETENTION_DAYS
    )
    root = default_claude_config_dir() / "shell-snapshots"
    outcome = PruneOutcome("shell-snapshots", SAFE, dry_run)
    if not root.is_dir():
        return outcome
    cutoff = time.time() - days * 86_400
    try:
        for p in root.iterdir():
            try:
                st = p.stat()
                if st.st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            size = st.st_size if p.is_file() else _dir_stats(p)[0]
            outcome.targets.append(str(p))
            outcome.would_free_bytes += size
    except OSError:
        pass
    if not dry_run:
        for t in list(outcome.targets):
            p = Path(t)
            size = p.stat().st_size if p.is_file() else _dir_stats(p)[0]
            if p.is_file():
                try:
                    p.unlink()
                    outcome.removed_count += 1
                    outcome.freed_bytes += size
                except OSError as exc:
                    outcome.errors.append(f"{p}: {exc}")
            else:
                _rmtree_target(p, outcome, size)
    return outcome


def _prune_chat_history(home: Path, older_than_days: int | None, dry_run: bool) -> PruneOutcome:
    days = older_than_days if older_than_days is not None else _DEFAULT_CHAT_HISTORY_RETENTION_DAYS
    root = default_claude_config_dir() / "projects"
    outcome = PruneOutcome("chat-history", REVIEW, dry_run)
    if not root.is_dir():
        return outcome
    cutoff = time.time() - days * 86_400
    try:
        for p in root.rglob("*.jsonl"):
            try:
                st = p.stat()
                if st.st_mtime < cutoff:
                    outcome.targets.append(str(p))
                    outcome.would_free_bytes += st.st_size
            except OSError:
                continue
    except OSError:
        pass
    if not dry_run:
        for t in list(outcome.targets):
            p = Path(t)
            try:
                size = p.stat().st_size
                p.unlink()
                outcome.removed_count += 1
                outcome.freed_bytes += size
            except OSError as exc:
                outcome.errors.append(f"{p}: {exc}")
    return outcome


def _prune_partial(home: Path, dry_run: bool) -> PruneOutcome:
    # NOTE: `_partial_dir()` sits next to `default_claude_config_dir()`, which
    # is only under `home`/DATA_HOME for an installed build — a dev checkout's
    # claude-config lives at ~/.claude, structurally outside DATA_HOME (see
    # config.default_claude_config_dir). So this one deliberately does NOT
    # `_assert_under(home)`, unlike every worktree/node_modules target below
    # (those live under DATA_HOME by construction and always get that check).
    # The path itself is fully deterministic (no external/user input feeds
    # it), so there's no escape vector to guard against here.
    outcome = PruneOutcome("partial", SAFE, dry_run)
    p = _partial_dir()
    if not p.is_dir():
        return outcome
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return outcome
    if age < _PARTIAL_MIN_AGE_SECONDS:
        outcome.skipped.append(
            f"{p}: อายุน้อยกว่า {_PARTIAL_MIN_AGE_SECONDS}s อาจเป็นการคัดลอกที่กำลังรันอยู่"
        )
        return outcome
    size, _c = _dir_stats(p)
    outcome.targets.append(str(p))
    outcome.would_free_bytes = size
    if not dry_run:
        sweep_link_points(p)
        _rmtree_target(p, outcome, size)
    return outcome


def _prune_orphan_worktrees(home: Path, dry_run: bool, mgr: WorktreeManager) -> PruneOutcome:
    """SAFE-bucket orphans only: empty checkout or node_modules-only content,
    clean, with no branch commits still unmerged (#132). This is also what
    ``prune_orphan_worktrees_boot`` runs — the unattended boot sweep must
    never lose real work, so anything short of that goes to
    ``_prune_orphan_worktrees_review`` instead, which requires an explicit
    ``--level review --yes``."""
    outcome = PruneOutcome("orphan-worktrees", SAFE, dry_run)
    for wt_dir in find_worktree_dirs(home):
        info = classify_worktree(wt_dir, mgr, runtime_dir=home / "runtime")
        if not info["orphan"] or _orphan_prune_bucket(wt_dir, info) != SAFE:
            continue
        size, _c = _dir_stats(wt_dir)
        outcome.targets.append(str(wt_dir))
        outcome.would_free_bytes += size
        if not dry_run:
            sweep_link_points(wt_dir)
            _rmtree_target(_assert_under(wt_dir, home), outcome, size)
    return outcome


def _prune_orphan_worktrees_review(home: Path, dry_run: bool, mgr: WorktreeManager) -> PruneOutcome:
    """REVIEW-bucket orphans (#132): still hold source files, uncommitted
    changes, or an unmerged branch. Never auto-swept — only reachable via an
    explicit ``takkub prune --level review --category orphan-worktrees-review
    --yes``, and the CLI prints every target path before deleting."""
    outcome = PruneOutcome("orphan-worktrees-review", REVIEW, dry_run)
    for wt_dir in find_worktree_dirs(home):
        info = classify_worktree(wt_dir, mgr, runtime_dir=home / "runtime")
        if not info["orphan"] or _orphan_prune_bucket(wt_dir, info) != REVIEW:
            continue
        size, _c = _dir_stats(wt_dir)
        outcome.targets.append(str(wt_dir))
        outcome.would_free_bytes += size
        if not dry_run:
            sweep_link_points(wt_dir)
            _rmtree_target(_assert_under(wt_dir, home), outcome, size)
    return outcome


def _prune_node_modules(
    home: Path, dry_run: bool, include_live: bool, mgr: WorktreeManager
) -> PruneOutcome:
    outcome = PruneOutcome("node-modules", SAFE if not include_live else REVIEW, dry_run)
    nm = scan_node_modules(home, mgr)
    for entry in nm["entries"]:
        if entry["orphan"]:
            outcome.targets.append(entry["path"])
            outcome.would_free_bytes += entry["size_bytes"]
            if not dry_run:
                p = _assert_under(Path(entry["path"]), home)
                sweep_link_points(p)
                _rmtree_target(p, outcome, entry["size_bytes"])
        elif include_live:
            outcome.targets.append(entry["path"])
            outcome.would_free_bytes += entry["size_bytes"]
            if not dry_run:
                p = _assert_under(Path(entry["path"]), home)
                sweep_link_points(p)
                _rmtree_target(p, outcome, entry["size_bytes"])
        else:
            outcome.skipped.append(
                f"{entry['path']}: worktree ยังลงทะเบียนอยู่ (live) — ข้าม (ใส่ --include-live เพื่อบังคับลบ)"
            )
    return outcome


def _prune_graft_graphs(home: Path, dry_run: bool, include_live: bool) -> PruneOutcome:
    """Delete ORPHAN `graft-graphs/*` stores unconditionally (a source path
    no longer configured in projects.json, #146 follow-up — safe to reclaim
    outright: re-adding the same path later just triggers a fresh `graft
    build`). A LIVE store is only deleted with `--include-live` (H1(c) — a
    live store used to have NO reclaim path at all, however large it grew;
    same escape hatch as `node-modules`'s, since forcing a rebuild is the
    same recoverable cost either way).

    *home* is accepted only for call-site uniformity with the other
    `_prune_*` functions (and threaded into `scan_graft_graphs`, which
    itself ignores it — see that function's docstring); the real safety
    boundary for `_assert_under` is `graft_store.GRAFT_STORE_ROOT.parent`
    (the whole `graft-graphs/` dir, every instance namespace under it) —
    not the narrower current-instance `GRAFT_STORE_ROOT` itself, since a
    pre-H2 legacy leftover (`scan_graft_graphs`'s `_legacy_orphan_entries`)
    sits one level up as a sibling instance dir, still never DATA_HOME-
    relative either way.

    Deleting a store also deletes its paired staging mirror under
    `graft_store.GRAFT_STAGING_ROOT` (same `graph_key`, H1 follow-up,
    2026-08-06) — `entry["size_bytes"]` already folds the staging bytes in
    (`scan_graft_graphs`), so this second removal contributes no additional
    freed-bytes accounting, only an error entry if it fails to fully clear.
    """
    outcome = PruneOutcome("graft-graphs", SAFE if not include_live else REVIEW, dry_run)
    gg = scan_graft_graphs(home)
    for entry in gg["entries"]:
        if entry["orphan"] or include_live:
            outcome.targets.append(entry["path"])
            outcome.would_free_bytes += entry["size_bytes"]
            if not dry_run:
                p = _assert_under(Path(entry["path"]), graft_store.GRAFT_STORE_ROOT.parent)
                _rmtree_target(p, outcome, entry["size_bytes"])
                staging = graft_store.GRAFT_STAGING_ROOT / Path(entry["path"]).name
                if staging.is_dir():
                    staging = _assert_under(staging, graft_store.GRAFT_STAGING_ROOT)
                    ok, leftover = robust_rmtree(staging)
                    if not ok:
                        outcome.errors.append(
                            f"{staging}: staging mirror ลบไม่ครบ ({len(leftover)} รายการเหลือ)"
                        )
        else:
            outcome.skipped.append(
                f"{entry['path']}: ยัง configure อยู่ (live) — ข้าม (ใส่ --include-live เพื่อบังคับลบ)"
            )
    return outcome


def prune(
    categories: list[str] | None = None,
    *,
    level: str = SAFE,
    older_than_days: int | None = None,
    dry_run: bool = True,
    include_live: bool = False,
    data_home: Path | None = None,
) -> dict:
    """Delete (or, by default, just preview) the requested prune categories.

    Never touches a category classified ``never`` regardless of what is
    asked for — refuses with an explicit reason instead. ``level`` gates
    which of the requestable categories are in scope when *categories* is
    omitted (``safe`` = the default set only; ``review`` must be requested
    explicitly, never implied). Every deletion target is re-validated to
    live under DATA_HOME right before it is touched (`_assert_under`).
    """
    if level not in (SAFE, REVIEW):
        return {"ok": False, "msg": f"unknown --level {level!r} (must be safe|review)"}
    home = (data_home or DATA_HOME).resolve()

    # No explicit --category: default to every category at or under the
    # requested --level (never auto-includes `review` unless asked).
    requested = (
        list(categories)
        if categories
        else [c for c in VALID_CATEGORIES if _LEVEL_RANK[_category_level(c)] <= _LEVEL_RANK[level]]
    )

    refusals: list[str] = []
    accepted: list[str] = []
    for c in requested:
        if c not in VALID_CATEGORIES and c not in _NEVER_CATEGORIES:
            refusals.append(f"{c}: ไม่รู้จัก category นี้")
            continue
        cat_level = _category_level(c)
        if cat_level == NEVER:
            reason = _NEVER_CATEGORIES.get(c, "จัดอยู่ในระดับ never")
            refusals.append(f"{c}: ปฏิเสธ — {reason}")
            continue
        if _LEVEL_RANK[cat_level] > _LEVEL_RANK[level]:
            refusals.append(f"{c}: ต้องใช้ --level {cat_level} (ปัจจุบัน --level {level})")
            continue
        accepted.append(c)

    mgr = WorktreeManager()
    outcomes: list[PruneOutcome] = []
    for c in accepted:
        if c == "browser-profiles":
            outcomes.append(_prune_browser_profiles(home, older_than_days, dry_run))
        elif c == "transcripts":
            outcomes.append(_prune_transcripts(home, older_than_days, dry_run))
        elif c == "exports":
            outcomes.append(_prune_exports(home, older_than_days, dry_run))
        elif c == "shell-snapshots":
            outcomes.append(_prune_shell_snapshots(home, older_than_days, dry_run))
        elif c == "chat-history":
            outcomes.append(_prune_chat_history(home, older_than_days, dry_run))
        elif c == "partial":
            outcomes.append(_prune_partial(home, dry_run))
        elif c == "orphan-worktrees":
            outcomes.append(_prune_orphan_worktrees(home, dry_run, mgr))
        elif c == "orphan-worktrees-review":
            outcomes.append(_prune_orphan_worktrees_review(home, dry_run, mgr))
        elif c == "node-modules":
            outcomes.append(_prune_node_modules(home, dry_run, include_live, mgr))
        elif c == "graft-graphs":
            outcomes.append(_prune_graft_graphs(home, dry_run, include_live))

    total_would_free = sum(o.would_free_bytes for o in outcomes)
    total_freed = sum(o.freed_bytes for o in outcomes)
    return {
        "ok": not refusals or bool(accepted),
        "dry_run": dry_run,
        "data_home": str(home),
        "categories": [o.as_dict() for o in outcomes],
        "refusals": refusals,
        "total_would_free_bytes": total_would_free,
        "total_freed_bytes": total_freed,
    }


def prune_orphan_worktrees_boot(data_home: Path | None = None) -> int:
    """Auto-prune hook for cockpit boot: sweeps ONLY the SAFE-bucket subset
    of orphan (unregistered) worktree checkouts — an empty checkout or one
    containing nothing but node_modules, clean, with no unmerged branch
    commits (the same subset `takkub prune --category orphan-worktrees`
    deletes). Never touches a still-registered worktree, and never touches
    an orphan that still holds source files, uncommitted changes, or a
    branch with commits nobody merged yet (#132) — those need an explicit
    `takkub prune --level review --category orphan-worktrees-review --yes`.
    Returns the number removed. Best-effort: never raises.

    Kill switch: TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE (any value other than ""
    or "0"), same shape as TAKKUB_SKIP_GRAFT_BUILD / TAKKUB_SKIP_MCP_WARM —
    set by conftest.py for every test run. Every `classify_worktree` call
    inside `_prune_orphan_worktrees` runs real `git rev-parse` / `git
    worktree list` subprocesses via WorktreeManager (`find_worktree_dirs`
    finds ANY dir under `<data_home>/worktrees/**`, including a leftover
    real dev-machine checkout, not just test fixtures) — Orchestrator()
    construction calls this at boot, so an unguarded suite run would shell
    out to real git once per collected worktree on every test that builds
    an Orchestrator. Guarded here (not just at the orchestrator.py call
    site) so no caller can bypass it, mirroring the other two kill
    switches' rationale.
    """
    if os.environ.get("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", "").strip() not in ("", "0"):
        return 0
    home = (data_home or DATA_HOME).resolve()
    try:
        result = _prune_orphan_worktrees(home, dry_run=False, mgr=WorktreeManager())
    except Exception:
        return 0
    return result.removed_count
