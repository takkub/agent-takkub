"""EditorHost / editor_widget.py (#365 phase 2 — Monaco read-only).

Pure functions (containment, size cap, binary sniff, git-HEAD diff) are
tested directly, the way production code only ever calls them from a worker
thread (see project_file_index.py's own test file for the same convention).

EditorHost's lazy-create/destroy lifecycle is tested with a stub
`view_factory` instead of a real QWebEngineView — constructing a real one
used to hard-abort pytest even under QT_QPA_PLATFORM=offscreen on this
codebase (see tests/test_terminal_widget.py's note on the identical issue
with xterm.js's QWebEngineView). Root-caused and fixed in conftest.py's
`_qt_session_app` (#364 lever-1 follow-up, confirmed on Windows) — this
file still uses the stub for the bulk of the suite (faster, no renderer
process per test) but see `test_real_qwebengineview_construction_does_not_abort`
below for an opt-in real-construction smoke test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import editor_widget as ew
from agent_takkub.editor_widget import (
    MAX_EDITOR_FILE_BYTES,
    EditorHost,
    _js_str,
    build_diff_result,
    read_file_for_editor,
    read_head_blob,
)
from agent_takkub.file_watch_service import FileWatchService
from agent_takkub.project_file_index import PathEscapesRootsError

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _stop_file_watch_timer(monkeypatch):
    # Every EditorHost constructs its own FileWatchService (#365 phase 3),
    # whose `_timer` can arm from a real QFileSystemWatcher fileChanged
    # signal fired by these tests' own saves/reloads landing on real files —
    # same convention as test_project_explorer.py's git-service timer guard.
    finalize = stop_timers_after(monkeypatch, FileWatchService, "_timer")
    yield
    try:
        finalize()
    except RuntimeError:
        pass


def _wait_until(predicate, timeout_ms: int = 3000, step_ms: int = 25) -> None:
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        QTest.qWait(step_ms)
        elapsed += step_ms
    assert predicate(), "condition not met before timeout"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_repo_with_commit(repo: Path, rel_path: str, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    f = repo / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", "init")


# ── read_file_for_editor: containment, binary sniff, size cap ──────────────


class TestReadFileForEditor:
    def test_text_file_reads_cleanly(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "main.py"
        f.write_text("print('hi')\n", encoding="utf-8")

        result = read_file_for_editor(f, [root])

        assert result.text == "print('hi')\n"
        assert result.binary is False
        assert result.too_large is False
        assert result.language_hint == "py"

    def test_path_outside_roots_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")

        with pytest.raises(PathEscapesRootsError):
            read_file_for_editor(outside, [root])

    def test_binary_file_flagged_no_text(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "image.bin"
        f.write_bytes(b"\x89PNG\x00\x01\x02\x03")

        result = read_file_for_editor(f, [root])

        assert result.binary is True
        assert result.text is None

    def test_oversized_file_flagged_no_text(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "big.txt"
        f.write_text("a" * 100, encoding="utf-8")

        result = read_file_for_editor(f, [root], max_bytes=50)

        assert result.too_large is True
        assert result.text is None
        assert result.size == 100

    def test_size_at_cap_boundary_is_not_too_large(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "exact.txt"
        f.write_text("a" * 50, encoding="utf-8")

        result = read_file_for_editor(f, [root], max_bytes=50)

        assert result.too_large is False
        assert result.text == "a" * 50


# ── git-HEAD diff ────────────────────────────────────────────────────────


@pytest.fixture
def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


class TestReadHeadBlob:
    def test_committed_file_returns_head_content(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "version 1\n")
        (repo / "a.py").write_text("version 2 (uncommitted)\n", encoding="utf-8")

        head_text = read_head_blob(repo, repo / "a.py")

        assert head_text == "version 1\n"

    def test_uncommitted_file_returns_none(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "version 1\n")
        (repo / "b.py").write_text("new file\n", encoding="utf-8")

        assert read_head_blob(repo, repo / "b.py") is None

    def test_not_a_git_repo_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        f = plain / "a.py"
        f.write_text("x", encoding="utf-8")

        assert read_head_blob(plain, f) is None


class TestBuildDiffResult:
    def test_returns_original_and_modified_text(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "old\n")
        (repo / "a.py").write_text("new\n", encoding="utf-8")

        result = build_diff_result(repo, [repo], repo / "a.py")

        assert result.error is None
        assert result.original_text == "old\n"
        assert result.modified_text == "new\n"

    def test_path_outside_roots_yields_error(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "old\n")
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")

        result = build_diff_result(repo, [repo], outside)

        assert result.error is not None
        assert result.original_text is None

    def test_binary_current_file_yields_error(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.bin", "placeholder")
        (repo / "a.bin").write_bytes(b"\x00\x01\x02")

        result = build_diff_result(repo, [repo], repo / "a.bin")

        assert result.error == "binary_or_too_large"


# ── EditorHost: lazy create / single instance / destroy on empty ───────────


class _StubEditorView(QWidget):
    """Mimics _EditorWebView's public surface without a real QWebEngineView.

    Subclasses QWidget (not plain QObject) because EditorHost adds it to a
    real QVBoxLayout via `layout.addWidget(view)`."""

    diffRequested = pyqtSignal(str)
    openExternallyRequested = pyqtSignal(str)
    revealRequested = pyqtSignal(str)
    tabClosed = pyqtSignal(str)
    askAgentRequested = pyqtSignal(str, int, int, str, str)
    saveRequested = pyqtSignal(str, str)
    keepMineRequested = pyqtSignal(str, str)
    reloadRequested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.run_js_calls: list[str] = []
        self.destroyed_called = False
        self.focus_calls = 0

    def run_js(self, script: str) -> None:
        self.run_js_calls.append(script)

    def focus(self) -> None:
        self.focus_calls += 1

    def destroy(self) -> None:
        self.destroyed_called = True


@pytest.fixture
def container(qapp) -> QWidget:
    return QWidget()


@pytest.fixture
def stub_factory():
    created: list[_StubEditorView] = []

    def factory():
        view = _StubEditorView()
        created.append(view)
        return view

    factory.created = created
    return factory


@pytest.fixture(autouse=True)
def _stub_project_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(ew, "project_roots", lambda name: {"main": tmp_path})
    return tmp_path


class TestLazyCreateAndSingleInstance:
    def test_no_view_until_first_open(self, container, stub_factory) -> None:
        host = EditorHost(container, view_factory=stub_factory)
        assert host.has_view() is False
        assert stub_factory.created == []

    def test_open_file_lazily_creates_the_view(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("print(1)\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)

        assert host.has_view() is True
        assert len(stub_factory.created) == 1
        view = stub_factory.created[0]
        assert any("editorOpenFile" in call for call in view.run_js_calls)

    def test_open_file_focuses_the_view(self, container, stub_factory, tmp_path) -> None:
        """A freshly opened file must actually receive keyboard focus
        (cursor blinking, ready to type) rather than only getting it if the
        user happens to click again afterward."""
        f = tmp_path / "a.py"
        f.write_text("print(1)\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)

        view = stub_factory.created[0]
        assert view.focus_calls >= 1

    def test_opening_a_second_file_reuses_the_same_view(
        self, container, stub_factory, tmp_path
    ) -> None:
        f1 = tmp_path / "a.py"
        f1.write_text("x", encoding="utf-8")
        f2 = tmp_path / "b.py"
        f2.write_text("y", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f1))
        _wait_until(lambda: host.open_count() == 1)
        host.open_file("proj", str(f2))
        _wait_until(lambda: host.open_count() == 2)

        assert len(stub_factory.created) == 1  # never a second WebView

    def test_binary_file_sends_unavailable_not_open(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "img.bin"
        f.write_bytes(b"\x00\x01\x02")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)

        view = stub_factory.created[0]
        assert any("editorOpenUnavailable" in call for call in view.run_js_calls)
        assert not any("editorOpenFile" in call for call in view.run_js_calls)

    def test_path_escaping_roots_emits_fileOpenFailed(
        self, container, stub_factory, tmp_path
    ) -> None:
        outside = tmp_path.parent / "definitely-outside.py"
        outside.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        failures: list[tuple[str, str]] = []
        host.fileOpenFailed.connect(lambda path, err: failures.append((path, err)))

        host.open_file("proj", str(outside))
        _wait_until(lambda: len(failures) == 1)

        assert host.has_view() is True  # view was still created before the worker ran
        assert host.open_count() == 0


class TestDestroyOnEmpty:
    def test_closing_last_tab_destroys_the_view(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        emptied: list[bool] = []
        host.emptied.connect(lambda: emptied.append(True))

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]

        # JS always echoes back the (already-resolved) path Python sent it
        # in editorOpenFile — this is the key `_open_paths` is tracked by.
        view.tabClosed.emit(str(f.resolve()))

        assert host.open_count() == 0
        assert host.has_view() is False
        assert view.destroyed_called is True
        assert emptied == [True]

    def test_closing_one_of_two_tabs_keeps_the_view_alive(
        self, container, stub_factory, tmp_path
    ) -> None:
        f1 = tmp_path / "a.py"
        f1.write_text("x", encoding="utf-8")
        f2 = tmp_path / "b.py"
        f2.write_text("y", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f1))
        _wait_until(lambda: host.open_count() == 1)
        host.open_file("proj", str(f2))
        _wait_until(lambda: host.open_count() == 2)
        view = stub_factory.created[0]

        view.tabClosed.emit(str(f1.resolve()))

        assert host.open_count() == 1
        assert host.has_view() is True
        assert view.destroyed_called is False

    def test_reopening_after_destroy_creates_a_fresh_view(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        stub_factory.created[0].tabClosed.emit(str(f.resolve()))
        assert host.has_view() is False

        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)

        assert host.has_view() is True
        assert len(stub_factory.created) == 2


class TestBridgeRouting:
    def test_diff_requested_routes_to_view(
        self, container, stub_factory, tmp_path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        _init_repo_with_commit(tmp_path, "a.py", "old\n")
        (tmp_path / "a.py").write_text("new\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(tmp_path / "a.py"))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        view.run_js_calls.clear()

        view.diffRequested.emit(str((tmp_path / "a.py").resolve()))
        _wait_until(lambda: any("editorShowDiff" in c for c in view.run_js_calls))

    def test_open_externally_refused_for_unknown_path(
        self, container, stub_factory, tmp_path, monkeypatch
    ) -> None:
        host = EditorHost(container, view_factory=stub_factory)
        # Force a view without going through open_file, so _open_paths is empty.
        host._ensure_view()
        calls: list[str] = []
        monkeypatch.setattr(
            ew.QDesktopServices, "openUrl", lambda url: calls.append(url.toString())
        )

        stub_factory.created[0].openExternallyRequested.emit(str(tmp_path / "never-opened.py"))

        assert calls == []

    def test_open_externally_opens_a_known_path(
        self, container, stub_factory, tmp_path, monkeypatch
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        calls: list[QUrl] = []
        monkeypatch.setattr(ew.QDesktopServices, "openUrl", lambda url: calls.append(url))

        stub_factory.created[0].openExternallyRequested.emit(str(f.resolve()))

        assert len(calls) == 1
        assert Path(calls[0].toLocalFile()) == f.resolve()

    def test_ask_agent_forwards_with_project_name(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj-x", str(f))
        _wait_until(lambda: host.open_count() == 1)
        received: list[tuple] = []
        host.askAgentRequested.connect(lambda *args: received.append(args))

        stub_factory.created[0].askAgentRequested.emit(
            str(f.resolve()), 1, 3, "sel", "explain this"
        )

        assert received == [("proj-x", str(f.resolve()), 1, 3, "sel", "explain this")]


def test_max_editor_file_bytes_is_a_sane_positive_bound() -> None:
    assert 0 < MAX_EDITOR_FILE_BYTES <= 10_000_000


def test_js_str_escapes_line_and_paragraph_separators() -> None:
    """U+2028/U+2029 are legal JSON string content but some JS parsers treat
    a raw (unescaped) occurrence as a literal line terminator even inside a
    string literal -- _js_str must always escape them, not just rely on
    json.dumps's ensure_ascii default staying True."""
    raw = "line1" + chr(0x2028) + "line2" + chr(0x2029) + "line3"

    payload = _js_str(raw)

    assert chr(0x2028) not in payload
    assert chr(0x2029) not in payload
    assert "\\u2028" in payload
    assert "\\u2029" in payload


# ── save / conflict / reload / disk-watch (#365 phase 3) ────────────────────


class TestSave:
    def test_successful_save_writes_disk_and_notifies_view(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        view.run_js_calls.clear()

        view.saveRequested.emit(str(f.resolve()), "new content\n")
        _wait_until(lambda: any("editorSaveResult" in c for c in view.run_js_calls))

        call = next(c for c in view.run_js_calls if "editorSaveResult" in c)
        assert "true" in call
        assert f.read_text(encoding="utf-8") == "new content\n"
        # Not a hardcoded byte count: save_atomic preserves the original
        # file's newline convention (04_MONACO_EDITOR_SPEC.md), and the
        # fixture write above went through pathlib's own platform-default
        # newline translation — f.stat().st_size is the actual disk truth.
        assert host._file_states[str(f.resolve())].size == f.stat().st_size

    def test_save_emits_git_refresh_needed_for_the_owning_project(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj-g", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        received: list[str] = []
        host.gitRefreshNeeded.connect(received.append)

        view.saveRequested.emit(str(f.resolve()), "y")
        _wait_until(lambda: received == ["proj-g"])

    def test_save_for_a_path_never_opened_is_ignored(
        self, container, stub_factory, tmp_path
    ) -> None:
        host = EditorHost(container, view_factory=stub_factory)
        host._ensure_view()
        view = stub_factory.created[0]

        view.saveRequested.emit(str(tmp_path / "never-opened.py"), "text")
        QTest.qWait(150)

        assert view.run_js_calls == []


class TestSaveConflict:
    def test_disk_changed_since_open_reports_conflict_with_disk_text(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        view.run_js_calls.clear()
        f.write_text("changed on disk\n", encoding="utf-8")

        view.saveRequested.emit(str(f.resolve()), "my edits\n")
        _wait_until(lambda: any("editorConflict" in c for c in view.run_js_calls))

        call = next(c for c in view.run_js_calls if "editorConflict" in c)
        assert "changed on disk" in call
        assert not any("editorSaveResult" in c for c in view.run_js_calls)
        assert f.read_text(encoding="utf-8") == "changed on disk\n"  # never overwritten silently

    def test_conflict_does_not_move_the_save_baseline(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())
        baseline_before = host._file_states[key]
        f.write_text("changed on disk\n", encoding="utf-8")

        view.saveRequested.emit(key, "my edits\n")
        _wait_until(lambda: any("editorConflict" in c for c in view.run_js_calls))

        assert host._file_states[key] == baseline_before


class TestKeepMine:
    def test_keep_mine_overwrites_despite_a_stale_baseline(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())
        f.write_text("changed on disk\n", encoding="utf-8")  # baseline is now stale

        view.keepMineRequested.emit(key, "forced overwrite\n")
        _wait_until(lambda: any("editorSaveResult" in c for c in view.run_js_calls))

        assert any("true" in c for c in view.run_js_calls if "editorSaveResult" in c)
        assert f.read_text(encoding="utf-8") == "forced overwrite\n"


class TestReloadFromDisk:
    def test_reload_sends_fresh_disk_content_and_updates_baseline(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())
        f.write_text("changed on disk\n", encoding="utf-8")
        view.run_js_calls.clear()

        view.reloadRequested.emit(key)
        _wait_until(lambda: any("editorReloadDisk" in c for c in view.run_js_calls))

        call = next(c for c in view.run_js_calls if "editorReloadDisk" in c)
        assert "changed on disk" in call
        assert host._file_states[key].size == f.stat().st_size

    def test_reload_for_unknown_path_is_ignored(self, container, stub_factory, tmp_path) -> None:
        host = EditorHost(container, view_factory=stub_factory)
        host._ensure_view()
        view = stub_factory.created[0]

        view.reloadRequested.emit(str(tmp_path / "never-opened.py"))
        QTest.qWait(150)

        assert view.run_js_calls == []


class TestDiskWatch:
    def test_external_change_notifies_the_view(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        view.run_js_calls.clear()

        f.write_text("changed externally\n", encoding="utf-8")
        host._file_watch._on_file_changed(str(f.resolve()))
        _wait_until(lambda: any("editorDiskChanged" in c for c in view.run_js_calls))

    def test_own_save_does_not_trigger_the_disk_changed_banner(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())

        view.saveRequested.emit(key, "my own edit\n")
        _wait_until(lambda: any("editorSaveResult" in c for c in view.run_js_calls))
        view.run_js_calls.clear()

        # The watcher's own debounced notification for the write we just
        # made — must be recognized as an echo, not an external edit.
        host._file_watch._on_file_changed(key)
        QTest.qWait(200)

        assert not any("editorDiskChanged" in c for c in view.run_js_calls)

    def test_deleted_file_notifies_removed(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())
        view.run_js_calls.clear()

        f.unlink()
        host._file_watch._on_file_changed(key)
        _wait_until(lambda: any("editorDiskRemoved" in c for c in view.run_js_calls))

    def test_closing_a_tab_unwatches_it(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)
        host.open_file("proj", str(f))
        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        key = str(f.resolve())
        assert key in host._file_watch.watched_paths()

        view.tabClosed.emit(key)

        assert key not in host._file_watch.watched_paths()


class TestOpenWithDiff:
    def test_show_diff_true_also_requests_the_diff(
        self, container, stub_factory, tmp_path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        _init_repo_with_commit(tmp_path, "a.py", "old\n")
        (tmp_path / "a.py").write_text("new\n", encoding="utf-8")
        host = EditorHost(container, view_factory=stub_factory)

        host.open_file("proj", str(tmp_path / "a.py"), show_diff=True)

        _wait_until(lambda: host.open_count() == 1)
        view = stub_factory.created[0]
        _wait_until(lambda: any("editorShowDiff" in c for c in view.run_js_calls))


# ── real QWebEngineView smoke (#364 lever-1 follow-up) ──────────────────────
#
# opt-in only (set AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1) — conftest.py's
# `_qt_session_app` now constructs QApplication with `[sys.argv[0]]` instead
# of `[]`, which fixed the hard-abort documented in this module's docstring
# (repro'd directly: docs/audit/2026-08-23-364-lever1-pane-discard-spike.md).
# That fix is confirmed on Windows only — a real QWebEngineView spinning up
# its renderer process under `QT_QPA_PLATFORM=offscreen` on CI's macos/ubuntu
# runners (no display, no xvfb) is unverified territory, so this stays out of
# the default gate until someone confirms it green there too. Run it directly
# with the env var set to check.
@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — see comment above; unverified on CI's macos/ubuntu runners",
)
def test_real_qwebengineview_construction_does_not_abort(qapp) -> None:
    """Exercises the exact construction path (`_EditorWebView.__init__` ->
    `QWebEngineView(self)` + `.load(...)`) that used to hard-abort the whole
    pytest process (Windows exit -1073740791) before the argv fix — the rest
    of this file only ever exercises `_StubEditorView`, which never touches
    real Qt WebEngine code at all."""
    view = ew._EditorWebView()
    try:
        QTest.qWait(500)  # let the renderer process actually spawn
        assert view._view is not None
    finally:
        view.destroy()
        QTest.qWait(50)


@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — see comment above; unverified on CI's macos/ubuntu runners",
)
def test_mouse_press_and_focus_in_forward_focus_to_view_and_monaco(qapp) -> None:
    """Regression: clicking into Monaco did nothing — the QWebEngineView
    reached Qt-level `hasFocus() == True` on click, but Chromium's DOM focus
    never moved into Monaco's own hidden input target, so keystrokes went
    nowhere. Reproduced directly on a real, on-screen `_EditorWebView`:
    `mousePressEvent` forwards, but `focusInEvent` does NOT fire from a raw
    click on the child view (Qt doesn't propagate focus-in to ancestors
    that way) — both are still wired, mirroring terminal_widget.py's
    identical belt-and-suspenders
    pattern for xterm.js, so a programmatic `.setFocus()` elsewhere (e.g.
    EditorHost.focus() on file-open) is covered too."""
    view = ew._EditorWebView()
    try:
        QTest.qWait(500)  # let the renderer process actually spawn
        calls: list = []
        view._view.setFocus = lambda *a, **k: calls.append("view.setFocus")
        view.run_js = lambda script: calls.append(("run_js", script))

        view.show()
        QTest.qWaitForWindowExposed(view)

        QTest.mouseClick(view, Qt.MouseButton.LeftButton)
        assert "view.setFocus" in calls, "mousePressEvent did not forward Qt focus"
        assert any(isinstance(c, tuple) and "focusActiveEditor" in c[1] for c in calls), (
            "mousePressEvent did not ask JS to move Monaco's DOM focus"
        )

        calls.clear()
        view.setFocus()
        QTest.qWait(50)
        assert "view.setFocus" in calls, "focusInEvent did not forward Qt focus"
    finally:
        view.destroy()
        QTest.qWait(50)
