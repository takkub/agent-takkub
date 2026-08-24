"""Sandboxed, lazy filesystem service for the Project Explorer (#365 phase 1).

Everything in this module is a plain-Python + `QRunnable` service layer —
no widgets. `project_explorer.py` is the only caller.

Two jobs:
  * `ProjectFileIndex` — lists exactly one directory at a time on a
    `QThreadPool` worker thread (never the Qt main thread), filtered by a
    fixed ignore-name set plus that directory's ignore rules. Ignore rules
    prefer real `git check-ignore` (#374 GAP-011 — one batched `--stdin -z`
    call per directory listing, never one call per file) and fall back to
    the handwritten `.gitignore` chain below when the directory isn't a git
    repo, `git` isn't on PATH, or the call errors/times out — see
    `_git_check_ignore_batch`.
  * `GitStatusService` — debounced, background `git status --porcelain`
    for one project root, feeding the Explorer's M/A/D badges. Phase 1
    scope only: refresh-on-request. Phase 4 wires it to a real file
    watcher + adds the diff view (see `docs/plans/2026-08-23-master-dev-plan.md` §4).

Every path this module hands back has already passed `resolve_and_contain`
— the single containment gate for "is this path allowed to be touched at
all", used identically by directory listing and by the Explorer's context
menu actions (open externally / reveal / copy path).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ._win_console import SUBPROCESS_NO_WINDOW

logger = logging.getLogger(__name__)

# #375 regression (native access violation, reproduced via faulthandler
# inside both `subprocess._execute_child` and `ntpath.realpath`): calling
# `Path.resolve()` or `subprocess.run()` concurrently from more than one
# thread is not safe on this platform/Python combination. This module and
# git_changes_service.py each run several independent QRunnable workers —
# `_ListDirWorker`/`_GitStatusWorker` here on `QThreadPool.globalInstance()`,
# `_StatusWorker`/`_DiffWorker`/`_RepoDiscoveryWorker` there on that same
# global pool plus a dedicated one-thread pool — so no single per-pool cap
# is enough. Two separate locks, not one: `RESOLVE_LOCK` guards every
# `Path.resolve()` call, including ones made on the Qt *main* thread
# (`ProjectExplorer`/service constructors resolving configured roots), so it
# must only ever be held for one fast stat syscall — never share it with a
# `subprocess.run()` call, which can block for its full multi-second
# timeout and would freeze the UI if the main thread ever waited on it.
# `SUBPROCESS_LOCK` guards `subprocess.run()` instead, and is only ever
# taken from background QRunnable workers in this module and
# git_changes_service.py (never the main thread), so holding it for a whole
# git invocation is fine. git_changes_service.py imports both rather than
# keeping its own.
RESOLVE_LOCK = threading.Lock()
SUBPROCESS_LOCK = threading.Lock()


def _safe_resolve(path: Path) -> Path:
    with RESOLVE_LOCK:
        return path.resolve()


# Always hidden regardless of .gitignore — matches 03_PROJECT_EXPLORER_SPEC.md.
IGNORED_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        ".next",
        "dist",
        "build",
        "coverage",
        "runtime",
        "venv",
        ".venv",
        "__pycache__",
    }
)

_GIT_STATUS_TIMEOUT_S = 10.0


class PathEscapesRootsError(ValueError):
    """A path did not resolve under any of the project's configured roots
    (traversal, or a symlink pointing outside them)."""


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: Path
    is_dir: bool


def resolve_and_contain(path: Path, roots: Sequence[Path]) -> Path:
    """Resolve `path` (following symlinks) and require the result to land
    under one of `roots` (already-resolved absolute paths). Raises
    `PathEscapesRootsError` otherwise. The one containment check every
    filesystem-touching call in the Explorer goes through — directory
    listing, "open externally", "reveal", and "copy path" alike."""
    resolved = _safe_resolve(Path(path))
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise PathEscapesRootsError(f"{path} escapes configured project roots")


def _parse_gitignore(gitignore_path: Path) -> list[tuple[str, bool, bool]]:
    """Parse one `.gitignore` into `(pattern, is_negation, dir_only)`.

    Deliberately a subset, not a git reimplementation: literal names plus
    `fnmatch` globs (`*`, `?`, `[...]`), a trailing `/` marks dir-only, a
    leading `!` negates. No `**`, no anchoring semantics beyond "this
    directory and below" — this is the fallback used when real `git
    check-ignore` isn't available (see `_git_check_ignore_batch` / GAP-011),
    good enough to hide what a generated repo accumulates in that case."""
    try:
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    patterns: list[tuple[str, bool, bool]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        neg = stripped.startswith("!")
        if neg:
            stripped = stripped[1:]
        dir_only = stripped.endswith("/")
        if dir_only:
            stripped = stripped[:-1]
        stripped = stripped.lstrip("/")
        if stripped:
            patterns.append((stripped, neg, dir_only))
    return patterns


def _gitignore_hides(name: str, is_dir: bool, patterns: list[tuple[str, bool, bool]]) -> bool:
    hidden = False
    for pattern, neg, dir_only in patterns:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatch(name, pattern):
            hidden = not neg
    return hidden


def _gitignore_chain(directory: Path, root: Path | None) -> list[tuple[str, bool, bool]]:
    """Merge `.gitignore` patterns from `root` down to `directory`
    (root-most first, so a deeper `.gitignore` can override a shallower
    one — matches real git precedence)."""
    if root is None or root == directory:
        return _parse_gitignore(directory / ".gitignore")
    ancestors = [directory]
    cur = directory
    while cur != root:
        cur = cur.parent
        ancestors.append(cur)
    patterns: list[tuple[str, bool, bool]] = []
    for anc in reversed(ancestors):
        patterns.extend(_parse_gitignore(anc / ".gitignore"))
    return patterns


def _git_check_ignore_batch(directory: Path, candidates: list[FileEntry]) -> frozenset[str] | None:
    """Ask real `git check-ignore` which of `candidates` (already
    scandir-ed, all direct children of `directory`) it would hide — one
    batched `--stdin -z` call per directory listing, never one call per
    file (#374 GAP-011).

    Returns `None` — not "nothing is ignored" — when git can't answer at
    all: `directory` isn't inside a work tree (exit 128), `git` isn't on
    PATH, or the call times out. The caller falls back to the handwritten
    `.gitignore` chain in that case. Routed through `git_changes_service.
    _run_git` (lazy import — that module imports this one, so a top-level
    import here would cycle) so this shares that module's `_SUBPROCESS_LOCK`:
    concurrent `subprocess.run()` calls from separate QThreadPool workers
    (this index's own listing workers alongside `GitChangesService`/
    `RepoDiscoveryService`) reproducibly crashed the interpreter on Windows
    without it (#375)."""
    from .git_changes_service import _run_git

    stdin_input = "".join(f"{e.name}\0" for e in candidates)
    try:
        proc = _run_git(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=directory,
            stdin_input=stdin_input,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("project_file_index: git check-ignore failed for %s: %s", directory, exc)
        return None
    # 0 = at least one path is ignored, 1 = none are — both are git actually
    # having answered. Anything else (128 = not a repo / fatal error, ...)
    # means git couldn't answer at all.
    if proc.returncode not in (0, 1):
        return None
    return frozenset(name for name in (proc.stdout or "").split("\0") if name)


def _resolve_ignored_names(
    directory: Path, root: Path | None, candidates: list[FileEntry]
) -> frozenset[str]:
    if not candidates:
        return frozenset()
    git_result = _git_check_ignore_batch(directory, candidates)
    if git_result is not None:
        return git_result
    patterns = _gitignore_chain(directory, root)
    return frozenset(e.name for e in candidates if _gitignore_hides(e.name, e.is_dir, patterns))


def list_dir_sync(directory: Path, roots: Sequence[Path]) -> list[FileEntry]:
    """The actual (blocking) directory listing. Only ever call this from a
    worker thread (`_ListDirWorker.run`) or directly from a test — never
    from the Qt main thread. Lists exactly one level, never recurses."""
    roots = [_safe_resolve(Path(r)) for r in roots]
    directory = resolve_and_contain(Path(directory), roots)
    root = next((r for r in roots if directory == r or r in directory.parents), None)

    candidates: list[FileEntry] = []
    try:
        with os.scandir(directory) as it:
            for de in it:
                name = de.name
                if name in IGNORED_NAMES:
                    continue
                try:
                    is_dir = de.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                # Re-verify containment per entry, not just for ones
                # `os.DirEntry.is_symlink()` flags — on Windows an NTFS
                # junction (the link kind `worktree_manager`/`skill_scan`
                # already use elsewhere in this codebase, made without admin
                # rights) reports `is_symlink() == False` even though
                # `Path.resolve()` follows it like any other reparse point.
                # A symlink-only check would silently let a junction escape
                # the sandbox; resolving+containing every entry costs one
                # extra stat per row of a single non-recursive directory
                # listing, which is cheap enough to never skip.
                try:
                    resolve_and_contain(Path(de.path), roots)
                except (PathEscapesRootsError, OSError):
                    continue
                candidates.append(FileEntry(name=name, path=Path(de.path), is_dir=is_dir))
    except OSError as exc:
        logger.debug("project_file_index: scandir(%s) failed: %s", directory, exc)
        return []

    ignored = _resolve_ignored_names(directory, root, candidates)
    entries = [e for e in candidates if e.name not in ignored]
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def _safe_emit(signal, *args) -> None:
    """Tolerate a receiver QObject torn down (project tab closed) while a
    worker-thread job was still in flight — same guard as update_worker.py."""
    try:
        signal.emit(*args)
    except RuntimeError:
        logger.debug("project_file_index: receiver deleted before emit — dropping result")


class _ListDirSignals(QObject):
    finished = pyqtSignal(str, list)  # (directory str, list[FileEntry])
    failed = pyqtSignal(str, str)  # (directory str, error message)
    # #365 phase 10 diagnostics (13_PERFORMANCE_AND_QT_RULES.md rule 10):
    # (directory str, elapsed_ms, entry_count) — timed inside the worker
    # thread, never on the Qt main thread; the connected slot only stores
    # three numbers, same trivial-assignment tier as every other
    # QRunnable→QObject result slot in this codebase.
    timed = pyqtSignal(str, float, int)


class _ListDirWorker(QRunnable):
    """Runs `list_dir_sync` on a QThreadPool thread — the canonical
    QRunnable + sibling-QObject-signals pattern already used by
    update_worker.py (QRunnable itself can't carry signals)."""

    def __init__(self, directory: Path, roots: Sequence[Path]) -> None:
        super().__init__()
        self.directory = directory
        self.roots = list(roots)
        self.signals = _ListDirSignals()

    def run(self) -> None:  # called by QThreadPool
        key = str(self.directory)
        t0 = time.perf_counter()
        try:
            entries = list_dir_sync(self.directory, self.roots)
        except PathEscapesRootsError as exc:
            _safe_emit(self.signals.failed, key, str(exc))
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _safe_emit(self.signals.timed, key, elapsed_ms, len(entries))
        _safe_emit(self.signals.finished, key, entries)


class ProjectFileIndex(QObject):
    """Lazy per-directory listing for one project. `request_list` dispatches
    to `QThreadPool.globalInstance()` so expanding a tree node never blocks
    the Qt main thread. No cancellation plumbing: listing one directory is
    cheap and non-recursive, so a stale in-flight request for a node the
    caller no longer cares about is simply ignored by the caller."""

    dirListed = pyqtSignal(str, list)
    dirListFailed = pyqtSignal(str, str)

    def __init__(self, roots: Sequence[Path], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.roots = [_safe_resolve(Path(r)) for r in roots]
        # #365 phase 10 diagnostics (13_PERFORMANCE_AND_QT_RULES.md rule 10,
        # "tree scan time") — last completed scan only, not a history; cheap
        # attributes updated from `_on_worker_timed` (main thread, trivial).
        self._last_scan_dir: str | None = None
        self._last_scan_ms: float | None = None
        self._last_scan_entry_count: int = 0
        self._scan_count: int = 0

    def request_list(self, directory: Path) -> None:
        worker = _ListDirWorker(Path(directory), self.roots)
        worker.signals.finished.connect(self._on_worker_finished)
        worker.signals.failed.connect(self._on_worker_failed)
        worker.signals.timed.connect(self._on_worker_timed)
        QThreadPool.globalInstance().start(worker)

    def _on_worker_finished(self, path: str, entries: list) -> None:
        self.dirListed.emit(path, entries)

    def _on_worker_failed(self, path: str, error: str) -> None:
        self.dirListFailed.emit(path, error)

    def _on_worker_timed(self, path: str, elapsed_ms: float, entry_count: int) -> None:
        self._last_scan_dir = path
        self._last_scan_ms = elapsed_ms
        self._last_scan_entry_count = entry_count
        self._scan_count += 1

    def diagnostics(self) -> dict:
        """`takkub doctor --workspace`'s "tree scan time" row — last
        completed `request_list` only (no history kept)."""
        return {
            "last_scan_dir": self._last_scan_dir,
            "last_scan_ms": self._last_scan_ms,
            "last_scan_entry_count": self._last_scan_entry_count,
            "scan_count": self._scan_count,
        }


# ── git status (skeleton — real wiring lands in phase 4, #365 §4) ──────────


class _GitStatusSignals(QObject):
    finished = pyqtSignal(dict)  # {relative_posix_path: "M" | "A" | "D"}


class _GitStatusWorker(QRunnable):
    def __init__(self, repo_root: Path) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.signals = _GitStatusSignals()

    def run(self) -> None:  # called by QThreadPool
        result: dict[str, str] = {}
        try:
            with SUBPROCESS_LOCK:
                proc = subprocess.run(
                    ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_STATUS_TIMEOUT_S,
                    creationflags=SUBPROCESS_NO_WINDOW,
                )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    code = line[:2]
                    rel = line[3:].strip()
                    if not rel:
                        continue
                    if "?" in code or "A" in code:
                        letter = "A"
                    elif "D" in code:
                        letter = "D"
                    else:
                        letter = "M"
                    result[rel] = letter
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("git status worker: %s", exc)
        _safe_emit(self.signals.finished, result)


class GitStatusService(QObject):
    """Background, debounced `git status` for one project root.

    Phase 1 scope: `request_refresh()` (e.g. on tree-node expand) restarts a
    single-shot debounce timer; only the last call within `debounce_ms`
    actually runs `git status`. Phase 4 adds watcher-triggered refresh and
    a real diff view on top of this."""

    statusChanged = pyqtSignal(dict)

    def __init__(
        self, repo_root: Path, *, debounce_ms: int = 1500, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.repo_root = Path(repo_root)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._run)

    def request_refresh(self) -> None:
        self._timer.start()

    def _run(self) -> None:
        worker = _GitStatusWorker(self.repo_root)
        worker.signals.finished.connect(self._on_worker_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_worker_finished(self, result: dict) -> None:
        self.statusChanged.emit(result)
