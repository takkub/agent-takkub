"""Per-project disk watcher for files currently open in the editor (#365
phase 4).

`QFileSystemWatcher` watches individual open files directly (`addPath` per
file) — never a directory, so there's no recursive scan
(13_PERFORMANCE_AND_QT_RULES.md rule 1). Its `fileChanged` signal is a cheap
OS-level notification and fires on the Qt main thread, but turning it into a
fresh `EditorFileState` means a stat + full read + SHA-256
(`editor_service.stat_snapshot`) — that work is always dispatched to a
`QThreadPool` worker, never run inline in the signal handler (rule 2's "no
heavy IO on main thread" extends to this snapshot, same as
`git_changes_service.py`'s status/diff workers).

Debounced: some save strategies (temp-file + rename) fire `fileChanged`
2-3 times for one logical edit, and some drop the path from the watch list
entirely when the file is replaced rather than written in place — both are
handled here (coalesce into one flush per quiet window; re-`addPath` on
every signal if the watcher stopped tracking it).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from .editor_service import MAX_EDIT_FILE_BYTES, EditorFileState, _log_event, stat_snapshot
from .project_file_index import PathEscapesRootsError, resolve_and_contain

logger = logging.getLogger(__name__)

_DEBOUNCE_MS = 400


def _safe_emit(signal, *args) -> None:
    """Tolerate a receiver QObject torn down mid-flight — same guard as
    project_file_index.py's `_safe_emit`."""
    try:
        signal.emit(*args)
    except RuntimeError:
        logger.debug("file_watch_service: receiver deleted before emit — dropping result")


class _SnapshotSignals(QObject):
    finished = pyqtSignal(str, object)  # path, EditorFileState
    missing = pyqtSignal(str)  # path no longer exists (deleted)


class _SnapshotWorker(QRunnable):
    def __init__(self, path: Path, max_bytes: int) -> None:
        super().__init__()
        self.path = path
        self.max_bytes = max_bytes
        self.signals = _SnapshotSignals()

    def run(self) -> None:  # called by QThreadPool
        if not self.path.exists():
            _safe_emit(self.signals.missing, str(self.path))
            return
        try:
            state = stat_snapshot(self.path, self.max_bytes)
        except OSError as exc:
            logger.debug("file_watch_service: snapshot failed for %s: %s", self.path, exc)
            return
        _safe_emit(self.signals.finished, str(self.path), state)


class FileWatchService(QObject):
    """Watches exactly the files a caller (`watch`/`unwatch`) says are
    currently open in the editor for one project. Emits `diskChanged` with a
    fresh `EditorFileState` (for the conflict-check flow in
    `editor_service.save_atomic`) or `diskRemoved` when a watched file
    disappears from disk."""

    diskChanged = pyqtSignal(str, object)  # path, EditorFileState
    diskRemoved = pyqtSignal(str)  # path

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        max_bytes: int = MAX_EDIT_FILE_BYTES,
        debounce_ms: int = _DEBOUNCE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.roots = [Path(r).resolve() for r in roots]
        self._max_bytes = max_bytes
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._pending: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._flush)

    def add_roots(self, roots: Sequence[Path]) -> None:
        """Extend the containment allow-list. `EditorHost` is one app-wide
        instance spanning every open project (unlike a per-project service),
        so its roots accumulate as each project's files get opened rather
        than being fixed at construction."""
        for r in roots:
            resolved = Path(r).resolve()
            if resolved not in self.roots:
                self.roots.append(resolved)

    def watch(self, path: Path) -> None:
        try:
            resolved = resolve_and_contain(Path(path), self.roots)
        except PathEscapesRootsError:
            return
        key = str(resolved)
        if key not in self._watcher.files():
            self._watcher.addPath(key)

    def unwatch(self, path: Path) -> None:
        key = str(Path(path).resolve())
        if key in self._watcher.files():
            self._watcher.removePath(key)
        self._pending.discard(key)

    def watched_paths(self) -> list[str]:
        return list(self._watcher.files())

    def _on_file_changed(self, path: str) -> None:
        p = Path(path)
        # A replace-based save (write temp + rename over target) drops the
        # watch on some platforms — re-add so later edits keep being seen.
        if p.exists() and path not in self._watcher.files():
            self._watcher.addPath(path)
        self._pending.add(path)
        self._timer.start()

    def _flush(self) -> None:
        pending, self._pending = self._pending, set()
        for path in pending:
            p = Path(path)
            if not p.exists():
                self._on_removed(path)
                continue
            worker = _SnapshotWorker(p, self._max_bytes)
            worker.signals.finished.connect(self._on_snapshot_ready)
            worker.signals.missing.connect(self._on_removed)
            QThreadPool.globalInstance().start(worker)

    def _on_removed(self, path: str) -> None:
        self.diskRemoved.emit(path)
        _log_event("workspace.file.disk_changed", path=path, removed=True)

    def _on_snapshot_ready(self, path: str, state: EditorFileState) -> None:
        self.diskChanged.emit(path, state)
        _log_event("workspace.file.disk_changed", path=path)
