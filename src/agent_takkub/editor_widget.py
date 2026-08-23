"""EditorHost: the single, app-wide Monaco WebView (#365 phase 2 — read-only).

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
  JS → Python (via `_EditorBridge`, QWebChannel object `bridge`):
    requestDiff(path) / openExternally(path) / revealInExplorer(path) /
    notifyTabClosed(path) / askAgent(path, startLine, endLine, selectedText,
    request) [phase 3+ placeholder — wired, not yet actionable] / ready()

Phase 2 is read-only: every Monaco model is created with `readOnly: true`
(static/editor/index.html), and there is no `saveFile` bridge slot at all —
Ctrl+S writes land in phase 3 (`docs/plans/2026-08-23-master-dev-plan.md`
§4 phase table).

Security (12_SECURITY_THREAT_MODEL.md): every path this module touches goes
through `project_file_index.resolve_and_contain` against that project's
configured roots — the same containment gate the Explorer's own context-menu
actions use. `MAX_EDITOR_FILE_BYTES` bounds what ever reaches the renderer;
a binary or oversized file gets a read-only placeholder tab instead of its
content. File IO and the git-HEAD diff both run on a `QThreadPool` worker
thread (13_PERFORMANCE_AND_QT_RULES.md rule 1/2), never the Qt main thread.
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

from .project_explorer import project_roots
from .project_file_index import PathEscapesRootsError, resolve_and_contain

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static" / "editor"
_INDEX_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "index.html"))

# 2 MB — generous for source files, small enough that a single open never
# stalls the renderer or ships a giant string over the QWebChannel bridge.
MAX_EDITOR_FILE_BYTES = 2_000_000
_BINARY_SNIFF_BYTES = 8192
_GIT_TIMEOUT_S = 10.0


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


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def read_file_for_editor(
    path: Path, roots: Sequence[Path], max_bytes: int = MAX_EDITOR_FILE_BYTES
) -> OpenFileResult:
    """Containment-checked, size-capped, binary-sniffed file read.

    Raises `PathEscapesRootsError` if `path` doesn't resolve under any of
    `roots`. Never raises for a binary/too-large file — those come back as
    `text=None` with the corresponding flag set, so the caller can render a
    placeholder instead of dumping raw bytes into a text editor.
    """
    resolved = resolve_and_contain(path, roots)
    size = resolved.stat().st_size
    with resolved.open("rb") as fh:
        sample = fh.read(_BINARY_SNIFF_BYTES)
    binary = _looks_binary(sample)
    too_large = size > max_bytes
    text = None
    if not binary and not too_large:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    return OpenFileResult(
        path=resolved,
        text=text,
        language_hint=resolved.suffix.lstrip(".").lower(),
        binary=binary,
        too_large=too_large,
        size=size,
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
            timeout=_GIT_TIMEOUT_S,
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
    try:
        resolved = resolve_and_contain(abs_path, roots)
    except PathEscapesRootsError as exc:
        return DiffResult(
            path=Path(abs_path), original_text=None, modified_text=None, error=str(exc)
        )
    current = read_file_for_editor(resolved, roots, max_bytes)
    if current.binary or current.too_large:
        return DiffResult(
            path=resolved, original_text=None, modified_text=None, error="binary_or_too_large"
        )
    original = read_head_blob(repo_root, resolved)
    return DiffResult(path=resolved, original_text=original, modified_text=current.text, error=None)


def _reveal_in_file_manager(path: Path) -> None:
    """Show `path` (a file) in the OS file manager. Best-effort — same
    per-platform approach as project_explorer.py's own reveal action, kept
    as a separate small copy here rather than a shared import so
    project_file_index.py stays free of QtGui/QtWidgets (its documented
    "zero PyQt widget imports" contract — see that module's docstring)."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
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


# ---------------------------------------------------------------------------
# QWebChannel bridge — JS → Python calls
# ---------------------------------------------------------------------------


class _EditorBridge(QObject):
    """Object exposed to JS via QWebChannel as `bridge`."""

    diffRequested = pyqtSignal(str)  # path
    openExternallyRequested = pyqtSignal(str)  # path
    revealRequested = pyqtSignal(str)  # path
    tabClosedSig = pyqtSignal(str)  # path
    # path, startLine, endLine, selectedText, request — phase 3+ placeholder
    askAgentRequested = pyqtSignal(str, int, int, str, str)
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

    # -- introspection (also used by tests) ---------------------------
    def has_view(self) -> bool:
        return self._view is not None

    def open_count(self) -> int:
        return len(self._open_paths)

    # -- open ------------------------------------------------------------
    def open_file(self, project_name: str, abs_path: str) -> None:
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
            lambda result, proj=project_name: self._on_file_read(proj, result)
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

    def _on_file_read(self, project_name: str, result: OpenFileResult) -> None:
        if self._view is None:
            return  # destroyed before the worker finished (rapid open+close-all)
        path_str = str(result.path)
        self._open_paths[path_str] = project_name
        if result.binary or result.too_large:
            reason = "binary" if result.binary else "too_large"
            self._view.run_js(
                f"editorOpenUnavailable({json.dumps(path_str)}, {json.dumps(reason)}, "
                f"{result.size});"
            )
        else:
            self._view.run_js(
                f"editorOpenFile({json.dumps(path_str)}, {json.dumps(result.text)}, "
                f"{json.dumps(result.language_hint)});"
            )
        self.fileOpened.emit(project_name, path_str)

    def _on_file_failed(self, path: str, error: str) -> None:
        self.fileOpenFailed.emit(path, error)

    # -- close / RAM lifecycle -------------------------------------------
    def _on_tab_closed(self, path: str) -> None:
        self._open_paths.pop(path, None)
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
        self._open_paths.clear()
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
            self._view.run_js(
                f"editorDiffFailed({json.dumps(path_str)}, {json.dumps(result.error)});"
            )
            return
        self._view.run_js(
            f"editorShowDiff({json.dumps(path_str)}, {json.dumps(result.original_text or '')}, "
            f"{json.dumps(result.modified_text or '')});"
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

    # -- ask agent (phase 3+ placeholder) ----------------------------------
    def _on_ask_agent(
        self, path: str, start_line: int, end_line: int, selected_text: str, request: str
    ) -> None:
        project_name = self._open_paths.get(path, "")
        self.askAgentRequested.emit(
            project_name, path, start_line, end_line, selected_text, request
        )
