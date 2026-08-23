"""file_watch_service.py (#365 phase 4 — disk-change notifications for open
editor files). `_on_file_changed`/`_flush` are exercised directly (as
`QFileSystemWatcher.fileChanged` would call them) rather than relying on a
real OS filesystem-change notification firing inside the test sandbox,
which is unreliable across CI/containers — the same reasoning
`test_editor_widget.py` gives for driving `EditorHost` through its stub
view's signals instead of a real WebEngine round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from agent_takkub.editor_service import EditorFileState
from agent_takkub.file_watch_service import FileWatchService


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(predicate, timeout_ms: int = 3000, step_ms: int = 25) -> None:
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        QTest.qWait(step_ms)
        elapsed += step_ms
    assert predicate(), "condition not met before timeout"


# ── watch / unwatch / containment ───────────────────────────────────────


class TestWatchUnwatch:
    def test_watch_adds_to_watcher(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root])

        svc.watch(f)

        assert str(f.resolve()) in svc.watched_paths()

    def test_unwatch_removes_from_watcher(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root])
        svc.watch(f)

        svc.unwatch(f)

        assert str(f.resolve()) not in svc.watched_paths()

    def test_watch_outside_roots_is_ignored_not_raised(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")
        svc = FileWatchService([root])

        svc.watch(outside)  # must not raise PathEscapesRootsError

        assert svc.watched_paths() == []

    def test_watch_same_path_twice_is_idempotent(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root])

        svc.watch(f)
        svc.watch(f)

        assert svc.watched_paths() == [str(f.resolve())]


# ── debounce: rapid fileChanged coalesces into one flush ────────────────


class TestDebounce:
    def test_rapid_changes_to_same_path_flush_once(self, qapp, monkeypatch, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        flushes: list[set] = []
        original_flush = FileWatchService._flush

        def _patched_flush(self) -> None:
            flushes.append(set(self._pending))
            original_flush(self)

        # Patch before construction — the QTimer's `timeout.connect(self._flush)`
        # in __init__ binds to whatever `_flush` resolves to at connect time,
        # so a post-construction monkeypatch would never be seen by the signal.
        monkeypatch.setattr(FileWatchService, "_flush", _patched_flush)
        svc = FileWatchService([root], debounce_ms=30)

        key = str(f.resolve())
        try:
            svc._on_file_changed(key)
            svc._on_file_changed(key)
            svc._on_file_changed(key)
            # Bounded pump, not a fixed `qWait(150)`: on a loaded macOS CI
            # runner a 30 ms single-shot timer can take >150 ms to fire,
            # which both failed the assert and leaked the still-armed QTimer
            # into the #344/#345 leak check.
            _wait_until(lambda: len(flushes) >= 1)
            QTest.qWait(60)  # a second flush would have landed by now

            assert len(flushes) == 1
        finally:
            svc._timer.stop()

    def test_no_flush_before_debounce_window_elapses(self, qapp, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=500)
        received: list[str] = []
        svc.diskChanged.connect(lambda path, state: received.append(path))

        svc._on_file_changed(str(f.resolve()))
        QTest.qWait(50)

        assert received == []


# ── disk_changed / disk_removed emission ─────────────────────────────────


class TestDiskChangeEmission:
    def test_changed_file_emits_disk_changed_with_state(self, qapp, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("original", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=20)
        received: list[tuple[str, EditorFileState]] = []
        svc.diskChanged.connect(lambda path, state: received.append((path, state)))

        f.write_text("changed on disk", encoding="utf-8")
        svc._on_file_changed(str(f.resolve()))
        _wait_until(lambda: len(received) == 1)

        path, state = received[0]
        assert path == str(f.resolve())
        assert state.binary is False
        assert state.size == len(b"changed on disk")

    def test_deleted_file_emits_disk_removed(self, qapp, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=20)
        removed: list[str] = []
        svc.diskRemoved.connect(removed.append)

        f.unlink()
        svc._on_file_changed(str(f.resolve()))
        _wait_until(lambda: len(removed) == 1)

        assert removed[0] == str(f.resolve())

    def test_two_different_paths_both_flush(self, qapp, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f1 = root / "a.py"
        f1.write_text("x", encoding="utf-8")
        f2 = root / "b.py"
        f2.write_text("y", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=20)
        received: list[str] = []
        svc.diskChanged.connect(lambda path, state: received.append(path))

        svc._on_file_changed(str(f1.resolve()))
        svc._on_file_changed(str(f2.resolve()))
        _wait_until(lambda: len(received) == 2)

        assert set(received) == {str(f1.resolve()), str(f2.resolve())}

    def test_unicode_thai_path(self, qapp, tmp_path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "ไฟล์ ก.py"
        f.write_text("สวัสดี", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=20)
        received: list[tuple[str, EditorFileState]] = []
        svc.diskChanged.connect(lambda path, state: received.append((path, state)))

        f.write_text("สวัสดีใหม่", encoding="utf-8")
        svc._on_file_changed(str(f.resolve()))
        _wait_until(lambda: len(received) == 1)

        assert received[0][0] == str(f.resolve())


# ── diagnostics (#365 phase 10) ───────────────────────────────────────────


class TestDiagnostics:
    def test_reports_watched_and_debounce_ms(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=250)
        svc.watch(f)

        diag = svc.diagnostics()

        assert diag["watched_count"] == 1
        assert diag["debounce_ms"] == 250
        assert diag["pending_count"] == 0
        assert diag["debounce_pending"] is False

    def test_pending_count_reflects_unflushed_changes(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")
        svc = FileWatchService([root], debounce_ms=500)

        svc._on_file_changed(str(f.resolve()))

        diag = svc.diagnostics()
        assert diag["pending_count"] == 1
        assert diag["debounce_pending"] is True
