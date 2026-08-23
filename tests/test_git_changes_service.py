"""git_changes_service.py (#365 phase 4 — git changes + diff).

`parse_status_v2` / `changes_sync` / `diff_sync` are plain functions — tested
directly, the way production only ever calls them from a `QRunnable` worker
(never the Qt main thread, 13_PERFORMANCE_AND_QT_RULES.md rules 1/2).
`GitChangesService`'s debounce is tested the same way
`test_project_file_index.py` tests `GitStatusService`'s.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from agent_takkub.git_changes_service import (
    MAX_DIFF_FILE_BYTES,
    GitChangesService,
    changes_sync,
    diff_sync,
    parse_status_v2,
    read_head_blob,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _wait_until(predicate, timeout_ms: int = 5000, step_ms: int = 10) -> bool:
    """Poll `predicate` while pumping the Qt event loop, instead of a single
    fixed `qWait` — a starved CI worker can push a 30ms QTimer's actual
    delivery well past a flat wait without the debounce logic itself being
    wrong. Bounded so a genuine non-fire still fails the test."""
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        QTest.qWait(step_ms)
        elapsed += step_ms
    return predicate()


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


@pytest.fixture
def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── parse_status_v2: pure parser ────────────────────────────────────────


class TestParseStatusV2:
    def test_empty_output(self) -> None:
        assert parse_status_v2("") == []

    def test_modified_ordinary_entry(self) -> None:
        # "1 XY sub mH mI mW hH hI path"
        record = "1 .M N... 100644 100644 100644 abc123 abc123 src/app.py"
        out = parse_status_v2(record + "\0")

        assert len(out) == 1
        assert out[0].path == "src/app.py"
        assert out[0].status == "M"

    def test_added_index_entry(self) -> None:
        record = "1 A. N... 000000 100644 100644 000000 abc123 src/new.py"
        out = parse_status_v2(record + "\0")

        assert out[0].status == "A"

    def test_deleted_entry(self) -> None:
        record = "1 D. N... 100644 000000 000000 abc123 000000 src/gone.py"
        out = parse_status_v2(record + "\0")

        assert out[0].status == "D"

    def test_untracked_entry(self) -> None:
        out = parse_status_v2("? new_file.txt\0")

        assert len(out) == 1
        assert out[0].path == "new_file.txt"
        assert out[0].status == "A"

    def test_ignored_entry_dropped(self) -> None:
        out = parse_status_v2("! build/output.log\0")

        assert out == []

    def test_rename_entry_keeps_new_path_and_consumes_orig(self) -> None:
        # "2 XY sub mH mI mW hH hI Xscore path\0origPath\0" then next record.
        rename = "2 R. N... 100644 100644 100644 abc123 abc123 R100 new_name.py"
        orig = "old_name.py"
        untracked = "? extra.txt"
        blob = f"{rename}\0{orig}\0{untracked}\0"

        out = parse_status_v2(blob)

        assert len(out) == 2
        assert out[0].path == "new_name.py"
        assert out[0].status == "R"
        assert out[1].path == "extra.txt"
        assert out[1].status == "A"

    def test_multiple_records_in_one_blob(self) -> None:
        blob = (
            "1 .M N... 100644 100644 100644 abc abc a.py\0"
            "1 D. N... 100644 000000 000000 abc 000 b.py\0"
            "? c.py\0"
        )

        out = parse_status_v2(blob)

        statuses = {c.path: c.status for c in out}
        assert statuses == {"a.py": "M", "b.py": "D", "c.py": "A"}

    def test_unicode_thai_path(self) -> None:
        out = parse_status_v2("? ไฟล์ใหม่.txt\0")

        assert out[0].path == "ไฟล์ใหม่.txt"


# ── changes_sync: real git subprocess ────────────────────────────────────


class TestChangesSync:
    def test_reports_modified_added_deleted_and_untracked(
        self, tmp_path: Path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "keep.py", "a\n")
        _init_repo_with_commit(repo, "to_delete.py", "b\n")
        (repo / "keep.py").write_text("a changed\n", encoding="utf-8")
        (repo / "to_delete.py").unlink()
        (repo / "untracked.py").write_text("new\n", encoding="utf-8")

        result = changes_sync(repo)

        statuses = {c.path: c.status for c in result}
        assert statuses["keep.py"] == "M"
        assert statuses["to_delete.py"] == "D"
        assert statuses["untracked.py"] == "A"

    def test_reports_rename(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "old.py", "content\n" * 20)
        _git(repo, "mv", "old.py", "new.py")

        result = changes_sync(repo)

        assert any(c.path == "new.py" and c.status == "R" for c in result)

    def test_not_a_git_repo_returns_empty(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert changes_sync(plain) == []

    def test_unicode_thai_filename(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "base.py", "x\n")
        (repo / "ไฟล์ไทย.py").write_text("สวัสดี\n", encoding="utf-8")

        result = changes_sync(repo)

        assert any(c.path == "ไฟล์ไทย.py" and c.status == "A" for c in result)


# ── diff_sync ─────────────────────────────────────────────────────────


class TestDiffSync:
    def test_modified_file_diff(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "old\n")
        (repo / "a.py").write_text("new\n", encoding="utf-8")

        result = diff_sync(repo, [repo], repo / "a.py")

        assert result.error is None
        assert "-old" in result.unified
        assert "+new" in result.unified

    def test_new_untracked_file_diff(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "base.py", "x\n")
        (repo / "fresh.py").write_text("brand new\n", encoding="utf-8")

        result = diff_sync(repo, [repo], repo / "fresh.py")

        assert result.error is None
        assert "+brand new" in result.unified

    def test_deleted_file_diff(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "gone.py", "was here\n")
        (repo / "gone.py").unlink()

        result = diff_sync(repo, [repo], repo / "gone.py")

        assert result.error is None
        assert "-was here" in result.unified

    def test_path_outside_roots_yields_error(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.py", "x\n")
        outside = tmp_path / "outside.py"
        outside.write_text("y", encoding="utf-8")

        result = diff_sync(repo, [repo], outside)

        assert result.error is not None
        assert result.unified is None

    def test_binary_current_file_yields_error(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.bin", "placeholder")
        (repo / "a.bin").write_bytes(b"\x00\x01\x02")

        result = diff_sync(repo, [repo], repo / "a.bin")

        assert result.error == "binary"

    def test_too_large_current_file_yields_error(self, tmp_path: Path, git_available) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "a.txt", "small\n")
        (repo / "a.txt").write_text("a" * 100, encoding="utf-8")

        result = diff_sync(repo, [repo], repo / "a.txt", max_bytes=10)

        assert result.error == "too_large"

    def test_untracked_binary_file_new_yields_error_not_no_content(
        self, tmp_path: Path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo, "base.py", "x\n")
        (repo / "new.bin").write_bytes(b"\x00\x01")

        result = diff_sync(repo, [repo], repo / "new.bin")

        assert result.error == "binary"


class TestReadHeadBlob:
    def test_not_a_git_repo_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert read_head_blob(plain, "a.py") is None


# ── GitChangesService: debounce ──────────────────────────────────────────


class TestGitChangesServiceDebounce:
    def test_rapid_refresh_calls_collapse_into_one_run(self, qapp, monkeypatch, tmp_path) -> None:
        calls: list[int] = []
        monkeypatch.setattr(GitChangesService, "_run", lambda self: calls.append(1))

        svc = GitChangesService(tmp_path, [tmp_path], debounce_ms=30)
        svc.request_refresh()
        svc.request_refresh()
        svc.request_refresh()

        assert _wait_until(lambda: calls == [1])
        assert calls == [1]

    def test_no_refresh_before_debounce_window_elapses(self, qapp, monkeypatch, tmp_path) -> None:
        calls: list[int] = []
        monkeypatch.setattr(GitChangesService, "_run", lambda self: calls.append(1))

        svc = GitChangesService(tmp_path, [tmp_path], debounce_ms=500)
        svc.request_refresh()
        QTest.qWait(50)

        assert calls == []


def test_max_diff_file_bytes_is_a_sane_positive_bound() -> None:
    assert 0 < MAX_DIFF_FILE_BYTES <= 10_000_000
