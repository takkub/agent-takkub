"""EditorHost: the single, app-wide Monaco WebView (#365 phase 2 read-only +
phase 3 safe edit: atomic save, dirty state, mtime+size+sha256 conflict
UI, disk-change watch).

RAM hard rule (`docs/plans/2026-08-23-master-dev-plan.md` §4, binding over
`04_MONACO_EDITOR_SPEC.md`/`13_PERFORMANCE_AND_QT_RULES.md`'s "one Monaco
WebView per project"): there is exactly ONE Monaco `QWebEngineView` for the
whole app, parked in a fixed dock outside every `ProjectTab` (MainWindow owns
the dock; `EditorHost` only owns the widget inside it). Opening a file from
a different project does not create a second WebView — it switches the
*content* (an internal Monaco tab + model) inside the one that exists.

Lifecycle: the WebView is created lazily on the first `open_file()` call and
fully torn down (`deleteLater` on the page + view) the moment the last
internal tab closes — so a session that never opens a file pays +0 RAM, and
the WebView never accumulates cross-project state. `EditorHost` takes an
optional `view_factory` for tests (a real `QWebEngineView` construction
hard-aborts pytest even under `QT_QPA_PLATFORM=offscreen` — see
`tests/test_terminal_widget.py`'s note on the same issue for xterm.js).

Wiring (mirrors `terminal_widget.py`'s Python↔JS split):
  Python → JS (via `EditorHost.run_js` → `page.runJavaScript`):
    editorOpenFile(path, text, languageHint)
    editorOpenUnavailable(path, reason, size)   — binary / too-large fallback
    editorShowDiff(path, originalText, modifiedText)
    editorDiffFailed(path, reason)
    editorSaveResult(path, ok, error)           — phase 3
    editorConflict(path, diskText)              — phase 3
    editorReloadDisk(path, text)                — phase 3
    editorDiskChanged(path) / editorDiskRemoved(path)  — phase 3 (file_watch_service)
  JS → Python (via `_EditorBridge`, QWebChannel object `bridge`):
    requestDiff(path) / openExternally(path) / revealInExplorer(path) /
    notifyTabClosed(path) / askAgent(path, startLine, endLine, selectedText,
    request) / saveFile(path, text) / keepMineSave(path, text) /
    reloadFromDisk(path) / ready()

Phase 3 (`editor_service.py`/`file_watch_service.py`, both backend-owned —
this module only calls them): Monaco models are editable, Ctrl+S sends
`bridge.saveFile(path, text)`. `EditorHost` tracks the last-known
`EditorFileState` per open path (`_file_states`, set on open/save — never
mutated by a disk-watch notification, see `_on_disk_changed`) and passes it
as `expected` to `editor_service.save_atomic`. A conflict answers back
`editorConflict(path, diskText)` (the current disk content, read fresh in
the same worker) so the JS side can offer `[Compare] [Reload disk] [Keep
mine]` without a second round-trip. "Keep mine" re-snapshots the disk state
at write time and forces the overwrite; "Reload disk" replaces the buffer
with fresh disk content and clears dirty. `FileWatchService` watches every
currently-open path and drives a disk-changed banner (JS decides whether to
show it; never an auto-reload) — `_on_disk_changed` diffs the incoming
snapshot against `_file_states` to tell an external edit apart from the
echo of our own save.

Security (12_SECURITY_THREAT_MODEL.md): every path this module touches goes
through `project_file_index.resolve_and_contain` against that project's
configured roots — the same containment gate the Explorer's own context-menu
actions use. `MAX_EDITOR_FILE_BYTES` bounds what ever reaches the renderer;
a binary or oversized file gets a read-only placeholder tab instead of its
content. File IO, the git-HEAD diff, and every save/reload both run on a
`QThreadPool` worker thread (13_PERFORMANCE_AND_QT_RULES.md rule 1/2), never
the Qt main thread.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ._win_console import SUBPROCESS_NO_WINDOW
from .editor_service import Conflict, EditorFileState, save_atomic, stat_snapshot
from .file_watch_service import FileWatchService
from .git_changes_service import find_rename_old_path
from .project_explorer import project_roots
from .project_file_index import PathEscapesRootsError, resolve_and_contain

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static" / "editor"
_INDEX_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "index.html"))

# 2 MB — generous for source files, small enough that a single open never
# stalls the renderer or ships a giant string over the QWebChannel bridge.
MAX_EDITOR_FILE_BYTES = 2_000_000
_GIT_TIMEOUT_S = 10.0


def _js_str(value: str | None) -> str:
    """`json.dumps` with `ensure_ascii` pinned explicitly true for every
    string embedded in a `run_js(f"...")` call. Every `run_js` call site in
    this module must go through this helper rather than a bare
    `json.dumps` — not just for non-ASCII round-tripping, but because
    U+2028/U+2029 (JS line/paragraph separators) are otherwise emitted as
    raw UTF-8 bytes inside the f-string's JS source text, which some
    embedded-JS parsers treat as a literal line terminator — silently
    truncating the string literal — even though they're legal inside a JSON
    string. `ensure_ascii=True` escapes both to `\\u2028`/`\\u2029`, which
    is safe in both JSON and JS string literals."""
    return json.dumps(value, ensure_ascii=True)


# ---------------------------------------------------------------------------
# Pure helpers — plain functions, no Qt. Always dispatched from a worker
# thread in production (see _OpenFileWorker/_DiffWorker below); called
# directly (no event loop needed) in tests, same convention as
# project_file_index.py's list_dir_sync.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenFileResult:
    path: Path
    text: str | None  # None when binary or too large
    language_hint: str
    binary: bool
    too_large: bool
    size: int
    state: EditorFileState  # versioned snapshot — the save-conflict baseline


def read_file_for_editor(
    path: Path, roots: Sequence[Path], max_bytes: int = MAX_EDITOR_FILE_BYTES
) -> OpenFileResult:
    """Containment-checked, size-capped, binary-sniffed file read.

    Raises `PathEscapesRootsError` if `path` doesn't resolve under any of
    `roots`. Never raises for a binary/too-large file — those come back as
    `text=None` with the corresponding flag set, so the caller can render a
    placeholder instead of dumping raw bytes into a text editor.

    Metadata (binary/too_large/size + the mtime_ns/sha256 conflict baseline)
    comes from `editor_service.stat_snapshot` — the same snapshot phase 3's
    save path checks disk against — so "what the editor thinks it opened"
    and "what a later Ctrl+S compares to" can never drift apart.
    """
    resolved = resolve_and_contain(path, roots)
    state = stat_snapshot(resolved, max_bytes)
    text = None
    if not state.binary and not state.too_large:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    return OpenFileResult(
        path=resolved,
        text=text,
        language_hint=resolved.suffix.lstrip(".").lower(),
        binary=state.binary,
        too_large=state.too_large,
        size=state.size,
        state=state,
    )


@dataclass(frozen=True)
class DiffResult:
    path: Path
    original_text: str | None  # None = no HEAD blob (new/untracked file)
    modified_text: str | None
    error: str | None  # set instead of the two texts above on failure


def read_head_blob(repo_root: Path, abs_path: Path) -> str | None:
    """`git show HEAD:<relpath>` — best-effort; None on any failure (new
    file not yet committed, not a git repo, detached HEAD with no commits,
    git not on PATH, ...). Mirrors project_file_index.py's GitStatusService
    subprocess pattern (timeout, swallow OSError)."""
    try:
        rel = abs_path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return None
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("editor_widget: git show HEAD:%s failed: %s", rel, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def build_diff_result(
    repo_root: Path,
    roots: Sequence[Path],
    abs_path: Path,
    max_bytes: int = MAX_EDITOR_FILE_BYTES,
) -> DiffResult:
    """Same per-status rules as `git_changes_service.diff_sync` (#375):
    a deleted file (`D`) is never stat'd/opened — `resolved.exists()` is
    checked first, so a diff-open on a git-deleted row reports the removal
    instead of crashing `stat_snapshot` on a missing path. A rename (`R`)
    falls back to `find_rename_old_path` once the direct HEAD:new_path
    lookup misses, the same way `diff_sync` does."""
    try:
        resolved = resolve_and_contain(abs_path, roots)
    except PathEscapesRootsError as exc:
        return DiffResult(
            path=Path(abs_path), original_text=None, modified_text=None, error=str(exc)
        )
    current_text: str | None = None
    if resolved.exists():
        current = read_file_for_editor(resolved, roots, max_bytes)
        if current.binary or current.too_large:
            return DiffResult(
                path=resolved, original_text=None, modified_text=None, error="binary_or_too_large"
            )
        current_text = current.text
    original = read_head_blob(repo_root, resolved)
    if original is None and current_text is not None:
        try:
            rel = resolved.relative_to(Path(repo_root).resolve()).as_posix()
        except ValueError:
            rel = None
        if rel is not None:
            old_rel = find_rename_old_path(repo_root, rel)
            if old_rel is not None:
                original = read_head_blob(repo_root, Path(repo_root) / old_rel)
    if original is None and current_text is None:
        return DiffResult(path=resolved, original_text=None, modified_text=None, error="no_content")
    return DiffResult(path=resolved, original_text=original, modified_text=current_text, error=None)


def _states_differ(a: EditorFileState | None, b: EditorFileState | None) -> bool:
    """Same comparison `editor_service._has_conflict` makes, kept as a local
    copy (not an import of that private helper) — used here only to decide
    whether a disk-watch notification is our own save echoing back (state
    unchanged) or a genuine external edit (state moved), never to gate a
    write itself."""
    if a is None or b is None:
        return True
    if a.mtime_ns != b.mtime_ns or a.size != b.size:
        return True
    if a.sha256 is not None and b.sha256 is not None:
        return a.sha256 != b.sha256
    return False


def _reveal_in_file_manager(path: Path) -> None:
    """Show `path` (a file) in the OS file manager. Best-effort — same
    per-platform approach as project_explorer.py's own reveal action, kept
    as a separate small copy here rather than a shared import so
    project_file_index.py stays free of QtGui/QtWidgets (its documented
    "zero PyQt widget imports" contract — see that module's docstring)."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"], creationflags=SUBPROCESS_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)], creationflags=SUBPROCESS_NO_WINDOW)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
    except OSError as exc:
        logger.debug("editor_widget: reveal failed for %s: %s", path, exc)


def _safe_emit(signal, *args) -> None:
    """Tolerate a receiver QObject torn down (editor closed mid-flight)
    while a worker-thread job was still in flight — same guard as
    project_file_index.py's `_safe_emit`."""
    try:
        signal.emit(*args)
    except RuntimeError:
        logger.debug("editor_widget: receiver deleted before emit — dropping result")


# ---------------------------------------------------------------------------
# QThreadPool workers — file read / git-HEAD diff never run on the Qt main
# thread (13_PERFORMANCE_AND_QT_RULES.md rules 1/2).
# ---------------------------------------------------------------------------


class _OpenFileSignals(QObject):
    finished = pyqtSignal(object)  # OpenFileResult
    failed = pyqtSignal(str, str)  # path, error


class _OpenFileWorker(QRunnable):
    def __init__(self, path: Path, roots: Sequence[Path], max_bytes: int) -> None:
        super().__init__()
        self.path = path
        self.roots = list(roots)
        self.max_bytes = max_bytes
        self.signals = _OpenFileSignals()

    def run(self) -> None:  # called by QThreadPool
        try:
            result = read_file_for_editor(self.path, self.roots, self.max_bytes)
        except (PathEscapesRootsError, OSError) as exc:
            _safe_emit(self.signals.failed, str(self.path), str(exc))
            return
        _safe_emit(self.signals.finished, result)


class _DiffSignals(QObject):
    finished = pyqtSignal(object)  # DiffResult


class _DiffWorker(QRunnable):
    def __init__(
        self, repo_root: Path, roots: Sequence[Path], abs_path: Path, max_bytes: int
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.roots = list(roots)
        self.abs_path = abs_path
        self.max_bytes = max_bytes
        self.signals = _DiffSignals()

    def run(self) -> None:  # called by QThreadPool
        result = build_diff_result(self.repo_root, self.roots, self.abs_path, self.max_bytes)
        _safe_emit(self.signals.finished, result)


class _SaveSignals(QObject):
    saved = pyqtSignal(str, object)  # path, EditorFileState
    conflict = pyqtSignal(str, object, object)  # path, Conflict, OpenFileResult|None
    failed = pyqtSignal(str, str)  # path, error


class _SaveWorker(QRunnable):
    """`force=True` (the "Keep mine" flow) re-snapshots the disk right
    before writing instead of trusting the possibly-stale `expected` the
    caller passed in — an intentional overwrite should race against *now*,
    not against whatever conflict was reported a moment earlier. A second,
    genuinely new conflict during that narrow window still comes back as
    `conflict`, handled by the same path as a normal save."""

    def __init__(
        self,
        path: Path,
        text: str,
        expected: EditorFileState | None,
        roots: Sequence[Path],
        max_bytes: int,
        *,
        force: bool = False,
    ) -> None:
        super().__init__()
        self.path = path
        self.text = text
        self.expected = expected
        self.roots = list(roots)
        self.max_bytes = max_bytes
        self.force = force
        self.signals = _SaveSignals()

    def run(self) -> None:  # called by QThreadPool
        expected = self.expected
        if self.force:
            try:
                resolved = resolve_and_contain(self.path, self.roots)
                expected = stat_snapshot(resolved, self.max_bytes) if resolved.exists() else None
            except (PathEscapesRootsError, OSError):
                pass  # fall through with the original `expected` — save_atomic re-checks anyway
        result = save_atomic(self.path, self.text, expected, self.roots, self.max_bytes)
        if result.ok:
            _safe_emit(self.signals.saved, str(self.path), result.state)
            return
        if result.conflict is not None:
            disk_read: OpenFileResult | None = None
            try:
                disk_read = read_file_for_editor(self.path, self.roots, self.max_bytes)
            except (PathEscapesRootsError, OSError) as exc:
                logger.debug("editor_widget: conflict disk-read failed for %s: %s", self.path, exc)
            _safe_emit(self.signals.conflict, str(self.path), result.conflict, disk_read)
            return
        _safe_emit(self.signals.failed, str(self.path), result.error or "save failed")


class _ReloadSignals(QObject):
    finished = pyqtSignal(str, object)  # path, OpenFileResult
    failed = pyqtSignal(str, str)  # path, error


class _ReloadWorker(QRunnable):
    def __init__(self, path: Path, roots: Sequence[Path], max_bytes: int) -> None:
        super().__init__()
        self.path = path
        self.roots = list(roots)
        self.max_bytes = max_bytes
        self.signals = _ReloadSignals()

    def run(self) -> None:  # called by QThreadPool
        try:
            result = read_file_for_editor(self.path, self.roots, self.max_bytes)
        except (PathEscapesRootsError, OSError) as exc:
            _safe_emit(self.signals.failed, str(self.path), str(exc))
            return
        _safe_emit(self.signals.finished, str(self.path), result)


# ---------------------------------------------------------------------------
# QWebChannel bridge — JS → Python calls
# ---------------------------------------------------------------------------


class _EditorBridge(QObject):
    """Object exposed to JS via QWebChannel as `bridge`."""

    diffRequested = pyqtSignal(str)  # path
    openExternallyRequested = pyqtSignal(str)  # path
    revealRequested = pyqtSignal(str)  # path
    tabClosedSig = pyqtSignal(str)  # path
    askAgentRequested = pyqtSignal(str, int, int, str, str)
    saveRequested = pyqtSignal(str, str)  # path, text
    keepMineRequested = pyqtSignal(str, str)  # path, text
    reloadRequested = pyqtSignal(str)  # path
    pageReady = pyqtSignal()

    @pyqtSlot(str)
    def requestDiff(self, path: str) -> None:
        self.diffRequested.emit(path)

    @pyqtSlot(str)
    def openExternally(self, path: str) -> None:
        self.openExternallyRequested.emit(path)

    @pyqtSlot(str)
    def revealInExplorer(self, path: str) -> None:
        self.revealRequested.emit(path)

    @pyqtSlot(str)
    def notifyTabClosed(self, path: str) -> None:
        self.tabClosedSig.emit(path)

    @pyqtSlot(str, int, int, str, str)
    def askAgent(
        self, path: str, start_line: int, end_line: int, selected_text: str, request: str
    ) -> None:
        self.askAgentRequested.emit(path, start_line, end_line, selected_text, request)

    @pyqtSlot(str, str)
    def saveFile(self, path: str, text: str) -> None:
        self.saveRequested.emit(path, text)

    @pyqtSlot(str, str)
    def keepMineSave(self, path: str, text: str) -> None:
        self.keepMineRequested.emit(path, text)

    @pyqtSlot(str)
    def reloadFromDisk(self, path: str) -> None:
        self.reloadRequested.emit(path)

    @pyqtSlot()
    def ready(self) -> None:
        self.pageReady.emit()


class _EditorWebView(QWidget):
    """Owns exactly one QWebEngineView + QWebChannel bridge, loaded from the
    local static/editor/index.html (no CDN — see 04_MONACO_EDITOR_SPEC.md).

    Never reparented after first paint — same Windows Chromium constraint
    terminal_widget.py documents for its own QWebEngineView. EditorHost
    creates a fresh instance per lazy-create cycle and fully destroys it on
    last-tab-close instead of hiding/reusing one across cycles.
    """

    diffRequested = pyqtSignal(str)
    openExternallyRequested = pyqtSignal(str)
    revealRequested = pyqtSignal(str)
    tabClosed = pyqtSignal(str)
    askAgentRequested = pyqtSignal(str, int, int, str, str)
    saveRequested = pyqtSignal(str, str)
    keepMineRequested = pyqtSignal(str, str)
    reloadRequested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QWebEngineView(self)
        layout.addWidget(self._view)

        self._channel = QWebChannel(self)
        self._bridge = _EditorBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._bridge.diffRequested.connect(self.diffRequested)
        self._bridge.openExternallyRequested.connect(self.openExternallyRequested)
        self._bridge.revealRequested.connect(self.revealRequested)
        self._bridge.tabClosedSig.connect(self.tabClosed)
        self._bridge.askAgentRequested.connect(self.askAgentRequested)
        self._bridge.saveRequested.connect(self.saveRequested)
        self._bridge.keepMineRequested.connect(self.keepMineRequested)
        self._bridge.reloadRequested.connect(self.reloadRequested)

        self._page_ready = False
        self._pending_js: list[str] = []
        self._bridge.pageReady.connect(self._on_page_ready)

        self._view.load(_INDEX_URL)

    def _on_page_ready(self) -> None:
        self._page_ready = True
        pending, self._pending_js = self._pending_js, []
        for script in pending:
            self._view.page().runJavaScript(script)

    def run_js(self, script: str) -> None:
        if self._page_ready:
            self._view.page().runJavaScript(script)
        else:
            self._pending_js.append(script)

    def focus(self) -> None:
        """Forward Qt widget focus AND Monaco's own DOM focus (a click
        landing on the QWebEngineView gives it `hasFocus() == True` at the
        Qt level, but never moves Chromium's DOM focus into Monaco's hidden
        input target on its own — so the cursor never blinks and keystrokes
        go nowhere). Same two-layer forward `terminal_widget.py`'s
        `mousePressEvent`/`focusInEvent` already does for xterm.js."""
        self._view.setFocus()
        self.run_js("if (window.focusActiveEditor) window.focusActiveEditor();")

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.focus()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focus()

    def destroy(self) -> None:
        """Full teardown so Chromium can release the renderer + JS heap.
        Distinct from hiding — used when the last open tab closes."""
        try:
            self._view.page().deleteLater()
        except RuntimeError:
            pass
        try:
            self._view.deleteLater()
        except RuntimeError:
            pass
        self.deleteLater()


# ---------------------------------------------------------------------------
# EditorHost — the app-wide singleton MainWindow owns
# ---------------------------------------------------------------------------


class EditorHost(QObject):
    """Lazily creates/destroys the one app-wide `_EditorWebView` inside a
    caller-owned container widget (MainWindow's editor dock).

    `view_factory` defaults to `_EditorWebView` (a real QWebEngineView);
    tests pass a stub with the same signal/`run_js`/`destroy` surface so
    lifecycle logic is exercised without constructing a real WebEngineView
    (which hard-aborts pytest on this codebase — see module docstring).
    """

    fileOpened = pyqtSignal(str, str)  # project_name, path
    fileOpenFailed = pyqtSignal(str, str)  # path, error
    emptied = pyqtSignal()  # last tab closed — view destroyed
    # project_name, path, startLine, endLine, selectedText, request
    askAgentRequested = pyqtSignal(str, str, int, int, str, str)
    # A save landed, or the watcher saw a genuine external edit — either way
    # the project's git-changes panel is now possibly stale (phase 4).
    gitRefreshNeeded = pyqtSignal(str)  # project_name

    def __init__(
        self,
        container: QWidget,
        *,
        view_factory: Callable[[], _EditorWebView] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        layout = container.layout()
        if layout is None:
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
        self._layout = layout
        self._view_factory = view_factory or _EditorWebView
        self._view: _EditorWebView | None = None
        self._open_paths: dict[str, str] = {}  # path -> project_name
        # Save-conflict baseline per open path — set on open/save only, never
        # by a disk-watch notification (see _on_disk_changed's module note).
        self._file_states: dict[str, EditorFileState] = {}
        # One app-wide watcher spanning every open project's roots (accumulated
        # via add_roots as files from new projects get opened) — mirrors this
        # host's own "one instance for the whole app" shape.
        self._file_watch = FileWatchService([], parent=self)
        self._file_watch.diskChanged.connect(self._on_disk_changed)
        self._file_watch.diskRemoved.connect(self._on_disk_removed)

    # -- introspection (also used by tests) ---------------------------
    def has_view(self) -> bool:
        return self._view is not None

    def open_count(self) -> int:
        return len(self._open_paths)

    def focus(self) -> None:
        """Give the WebView (and Monaco's own DOM focus) keyboard focus —
        called after a file opens and by MainWindow after showing/raising
        the editor dock, so the cursor is blinking and ready to type
        the moment a file appears, not only after a click forces it."""
        if self._view is not None:
            self._view.focus()

    # -- open ------------------------------------------------------------
    def open_file(self, project_name: str, abs_path: str, *, show_diff: bool = False) -> None:
        try:
            roots = list(project_roots(project_name).values())
        except Exception as exc:  # defensive — malformed projects.json entry
            logger.warning("editor_widget: project_roots(%s) failed: %s", project_name, exc)
            self.fileOpenFailed.emit(abs_path, str(exc))
            return
        if not roots:
            self.fileOpenFailed.emit(abs_path, "no configured roots for project")
            return
        self._ensure_view()
        worker = _OpenFileWorker(Path(abs_path), roots, MAX_EDITOR_FILE_BYTES)
        worker.signals.finished.connect(
            lambda result, proj=project_name, sd=show_diff: self._on_file_read(proj, result, sd)
        )
        worker.signals.failed.connect(self._on_file_failed)
        QThreadPool.globalInstance().start(worker)

    def _ensure_view(self) -> None:
        if self._view is not None:
            return
        self._view = self._view_factory()
        self._layout.addWidget(self._view)
        self._view.tabClosed.connect(self._on_tab_closed)
        self._view.diffRequested.connect(self._on_diff_requested)
        self._view.openExternallyRequested.connect(self._on_open_externally)
        self._view.revealRequested.connect(self._on_reveal)
        self._view.askAgentRequested.connect(self._on_ask_agent)
        self._view.saveRequested.connect(self._on_save_requested)
        self._view.keepMineRequested.connect(self._on_keep_mine_requested)
        self._view.reloadRequested.connect(self._on_reload_requested)

    def _on_file_read(
        self, project_name: str, result: OpenFileResult, show_diff: bool = False
    ) -> None:
        if self._view is None:
            return  # destroyed before the worker finished (rapid open+close-all)
        path_str = str(result.path)
        self._open_paths[path_str] = project_name
        self._file_states[path_str] = result.state
        if result.binary or result.too_large:
            reason = "binary" if result.binary else "too_large"
            self._view.run_js(
                f"editorOpenUnavailable({_js_str(path_str)}, {_js_str(reason)}, {result.size});"
            )
        else:
            self._view.run_js(
                f"editorOpenFile({_js_str(path_str)}, {_js_str(result.text)}, "
                f"{_js_str(result.language_hint)});"
            )
            try:
                roots = list(project_roots(project_name).values())
            except Exception:  # defensive — same guard as open_file above
                roots = []
            self._file_watch.add_roots(roots)
            self._file_watch.watch(result.path)
            self.focus()
        self.fileOpened.emit(project_name, path_str)
        if show_diff and not (result.binary or result.too_large):
            self._on_diff_requested(path_str)

    def _on_file_failed(self, path: str, error: str) -> None:
        self.fileOpenFailed.emit(path, error)

    # -- close / RAM lifecycle -------------------------------------------
    def _on_tab_closed(self, path: str) -> None:
        self._open_paths.pop(path, None)
        self._file_states.pop(path, None)
        self._file_watch.unwatch(Path(path))
        if not self._open_paths:
            self._destroy_view()

    def _destroy_view(self) -> None:
        if self._view is None:
            return
        view = self._view
        self._view = None
        self._layout.removeWidget(view)
        view.destroy()
        self.emptied.emit()

    def close_all(self) -> None:
        """Force-close every tab (e.g. app shutdown). No-op if already empty."""
        for path in list(self._open_paths):
            self._file_watch.unwatch(Path(path))
        self._open_paths.clear()
        self._file_states.clear()
        self._destroy_view()

    # -- diff --------------------------------------------------------------
    def _on_diff_requested(self, path: str) -> None:
        project_name = self._open_paths.get(path)
        if project_name is None:
            return
        try:
            roots = list(project_roots(project_name).values())
        except Exception:  # defensive — same guard as open_file
            return
        if not roots:
            return
        worker = _DiffWorker(roots[0], roots, Path(path), MAX_EDITOR_FILE_BYTES)
        worker.signals.finished.connect(self._on_diff_ready)
        QThreadPool.globalInstance().start(worker)

    def _on_diff_ready(self, result: DiffResult) -> None:
        if self._view is None:
            return
        path_str = str(result.path)
        if result.error:
            self._view.run_js(f"editorDiffFailed({_js_str(path_str)}, {_js_str(result.error)});")
            return
        self._view.run_js(
            f"editorShowDiff({_js_str(path_str)}, {_js_str(result.original_text or '')}, "
            f"{_js_str(result.modified_text or '')});"
        )

    # -- open externally / reveal ------------------------------------------
    def _on_open_externally(self, path: str) -> None:
        self._guarded(
            path, lambda resolved: QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved)))
        )

    def _on_reveal(self, path: str) -> None:
        self._guarded(path, _reveal_in_file_manager)

    def _guarded(self, path: str, action: Callable[[Path], None]) -> None:
        project_name = self._open_paths.get(path)
        if project_name is None:
            return
        try:
            roots = list(project_roots(project_name).values())
            resolved = resolve_and_contain(Path(path), roots)
        except Exception:  # defensive — never crash on a stale tab / bad projects.json
            return
        action(resolved)

    # -- ask agent -----------------------------------------------------------
    def _on_ask_agent(
        self, path: str, start_line: int, end_line: int, selected_text: str, request: str
    ) -> None:
        project_name = self._open_paths.get(path, "")
        self.askAgentRequested.emit(
            project_name, path, start_line, end_line, selected_text, request
        )

    # -- save / conflict (phase 3) --------------------------------------------
    def _on_save_requested(self, path: str, text: str) -> None:
        self._dispatch_save(path, text, force=False)

    def _on_keep_mine_requested(self, path: str, text: str) -> None:
        self._dispatch_save(path, text, force=True)

    def _dispatch_save(self, path: str, text: str, *, force: bool) -> None:
        project_name = self._open_paths.get(path)
        if project_name is None:
            return
        try:
            roots = list(project_roots(project_name).values())
        except Exception:  # defensive — same guard as open_file
            return
        if not roots:
            return
        expected = self._file_states.get(path)
        worker = _SaveWorker(Path(path), text, expected, roots, MAX_EDITOR_FILE_BYTES, force=force)
        worker.signals.saved.connect(self._on_save_ok)
        worker.signals.conflict.connect(self._on_save_conflict)
        worker.signals.failed.connect(self._on_save_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_save_ok(self, path: str, state: EditorFileState) -> None:
        self._file_states[path] = state
        if self._view is not None:
            self._view.run_js(f"editorSaveResult({_js_str(path)}, true, null);")
        project_name = self._open_paths.get(path)
        if project_name is not None:
            self.gitRefreshNeeded.emit(project_name)

    def _on_save_conflict(
        self, path: str, conflict: Conflict, disk_read: OpenFileResult | None
    ) -> None:
        if self._view is None:
            return
        disk_text = None
        if disk_read is not None and not disk_read.binary and not disk_read.too_large:
            disk_text = disk_read.text
        # Deliberately NOT updating self._file_states here — the baseline
        # stays the stale snapshot the buffer was actually opened/saved
        # against, so a later plain Ctrl+S (without going through
        # [Keep mine]) still correctly re-detects the same conflict instead
        # of silently overwriting on retry.
        self._view.run_js(f"editorConflict({_js_str(path)}, {_js_str(disk_text)});")

    def _on_save_failed(self, path: str, error: str) -> None:
        if self._view is not None:
            self._view.run_js(f"editorSaveResult({_js_str(path)}, false, {_js_str(error)});")

    # -- reload from disk (phase 3) -------------------------------------------
    def _on_reload_requested(self, path: str) -> None:
        project_name = self._open_paths.get(path)
        if project_name is None:
            return
        try:
            roots = list(project_roots(project_name).values())
        except Exception:  # defensive — same guard as open_file
            return
        if not roots:
            return
        worker = _ReloadWorker(Path(path), roots, MAX_EDITOR_FILE_BYTES)
        worker.signals.finished.connect(self._on_reload_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_reload_finished(self, path: str, result: OpenFileResult) -> None:
        if self._view is None:
            return
        self._file_states[path] = result.state
        if result.binary or result.too_large:
            reason = "binary" if result.binary else "too_large"
            self._view.run_js(
                f"editorOpenUnavailable({_js_str(path)}, {_js_str(reason)}, {result.size});"
            )
        else:
            self._view.run_js(f"editorReloadDisk({_js_str(path)}, {_js_str(result.text)});")

    # -- disk watch (phase 3, file_watch_service.py) --------------------------
    def _on_disk_changed(self, path: str, state: EditorFileState) -> None:
        baseline = self._file_states.get(path)
        if baseline is not None and not _states_differ(baseline, state):
            return  # matches our own last open/save — the watcher just echoed it back
        if self._view is not None:
            self._view.run_js(f"editorDiskChanged({_js_str(path)});")
        project_name = self._open_paths.get(path)
        if project_name is not None:
            self.gitRefreshNeeded.emit(project_name)

    def _on_disk_removed(self, path: str) -> None:
        if self._view is not None:
            self._view.run_js(f"editorDiskRemoved({_js_str(path)});")
