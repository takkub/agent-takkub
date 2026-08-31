"""Project Explorer service layer (#365 phase 1): containment, ignore
policy, .gitignore chain, and the debounced git-status skeleton.

`resolve_and_contain` / `list_dir_sync` are plain synchronous functions —
tested directly here, the way production code only ever calls them from a
worker thread (see `_ListDirWorker.run`), never the Qt main thread.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from agent_takkub import git_changes_service
from agent_takkub.project_file_index import (
    IGNORED_NAMES,
    GitStatusService,
    PathEscapesRootsError,
    ProjectFileIndex,
    list_dir_sync,
    resolve_and_contain,
)
from agent_takkub.worktree_manager import _make_link


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ── resolve_and_contain ─────────────────────────────────────────────────


class TestResolveAndContain:
    def test_path_under_root_is_ok(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        result = resolve_and_contain(root / "src", [root])
        assert result == (root / "src").resolve()

    def test_root_itself_is_ok(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        assert resolve_and_contain(root, [root]) == root.resolve()

    def test_sibling_outside_root_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(PathEscapesRootsError):
            resolve_and_contain(outside, [root])

    def test_dotdot_traversal_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (tmp_path / "outside").mkdir()
        traversal = root / ".." / "outside"
        with pytest.raises(PathEscapesRootsError):
            resolve_and_contain(traversal, [root])

    def test_matches_any_of_several_roots(self, tmp_path: Path) -> None:
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        assert resolve_and_contain(root_b, [root_a, root_b]) == root_b.resolve()


# ── list_dir_sync: ignore policy + .gitignore ───────────────────────────


class TestListDirSync:
    def test_default_ignored_names_hidden(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        for name in IGNORED_NAMES:
            (root / name).mkdir()
        (root / "src").mkdir()

        names = {e.name for e in list_dir_sync(root, [root])}
        assert names == {"src"}

    def test_gitignore_hides_matching_file(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".gitignore").write_text("*.log\n")
        (root / "debug.log").write_text("x")
        (root / "keep.txt").write_text("x")

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "debug.log" not in names
        assert "keep.txt" in names

    def test_gitignore_dir_only_pattern(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".gitignore").write_text("out/\n")
        (root / "out").mkdir()
        (root / "out.txt").write_text("x")  # not a dir — must survive dir-only pattern

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "out" not in names
        assert "out.txt" in names

    def test_gitignore_negation_unhides(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".gitignore").write_text("*.log\n!keep.log\n")
        (root / "debug.log").write_text("x")
        (root / "keep.log").write_text("x")

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "debug.log" not in names
        assert "keep.log" in names

    def test_gitignore_chain_from_ancestor_applies_deeper(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        (root / "sub").mkdir(parents=True)
        (root / ".gitignore").write_text("*.tmp\n")
        (root / "sub" / "scratch.tmp").write_text("x")
        (root / "sub" / "real.py").write_text("x")

        names = {e.name for e in list_dir_sync(root / "sub", [root])}
        assert "scratch.tmp" not in names
        assert "real.py" in names

    def test_directories_sort_before_files_case_insensitive(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / "Zebra.py").write_text("x")
        (root / "apple").mkdir()
        (root / "banana.py").write_text("x")

        names = [e.name for e in list_dir_sync(root, [root])]
        assert names == ["apple", "banana.py", "Zebra.py"]

    def test_path_outside_roots_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(PathEscapesRootsError):
            list_dir_sync(outside, [root])

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        assert list_dir_sync(root / "does-not-exist", [root]) == []

    def test_junction_escaping_root_is_hidden(self, tmp_path: Path) -> None:
        """Windows NTFS junctions (the same admin-free link kind
        `worktree_manager`/`skill_scan` use elsewhere) report
        `is_symlink() == False` — a symlink-only containment re-check would
        silently leak this. See adr-workspace-shell.md's security note."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "src").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        err = _make_link(outside, root / "escape_link")
        if err is not None:
            pytest.skip(f"link creation unavailable in this environment: {err}")

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "escape_link" not in names
        assert "src" in names


# ── list_dir_sync: git-native ignore parity (#374 GAP-011) ─────────────
# `_git` / `git_available` mirror test_git_changes_service.py's own helpers
# (kept local — this file already avoided importing that module's internals).


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.fixture
def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


class TestGitNativeIgnoreParity:
    def test_git_check_ignore_hides_a_doublestar_pattern_the_handwritten_chain_cannot(
        self, tmp_path: Path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q")
        # `**/build` — the handwritten `_parse_gitignore`/`_gitignore_hides`
        # chain has no `**` support (see its own docstring) and would fnmatch
        # this literally, never matching a plain top-level "build" — real git
        # does. Proves the git-native path is actually being consulted, not
        # just silently falling back.
        (root / ".gitignore").write_text("**/build\n")
        (root / "build").mkdir()
        (root / "keep.txt").write_text("x")

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "build" not in names
        assert "keep.txt" in names

    def test_ignored_names_stay_hidden_even_inside_a_real_repo(
        self, tmp_path: Path, git_available
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q")
        (root / "node_modules").mkdir()
        (root / "src").mkdir()

        names = {e.name for e in list_dir_sync(root, [root])}
        assert names == {"src"}

    def test_one_directory_listing_makes_exactly_one_git_call_not_one_per_file(
        self, tmp_path: Path, git_available, monkeypatch
    ) -> None:
        if not git_available:
            pytest.skip("git not on PATH")
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q")
        for i in range(5):
            (root / f"file{i}.txt").write_text("x")

        calls: list[list[str]] = []
        real_run_git = git_changes_service._run_git

        def _counting_run_git(args, **kwargs):
            calls.append(args)
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(git_changes_service, "_run_git", _counting_run_git)

        list_dir_sync(root, [root])

        assert len(calls) == 1
        assert calls[0][:3] == ["git", "check-ignore", "--stdin"]

    def test_git_unavailable_still_falls_back_correctly_inside_a_real_repo(
        self, tmp_path: Path, git_available, monkeypatch
    ) -> None:
        """Even inside a real repo, a git failure (missing binary, timeout,
        ...) must not lose ignore coverage — `_git_check_ignore_batch`
        returning None is the documented fallback signal, forced here
        directly rather than relying on git actually being absent."""
        if not git_available:
            pytest.skip("git not on PATH")
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q")
        (root / ".gitignore").write_text("*.log\n")
        (root / "debug.log").write_text("x")
        (root / "keep.txt").write_text("x")

        from agent_takkub import project_file_index as pfi

        monkeypatch.setattr(pfi, "_git_check_ignore_batch", lambda directory, candidates: None)

        names = {e.name for e in list_dir_sync(root, [root])}
        assert "debug.log" not in names
        assert "keep.txt" in names


# ── GitStatusService: debounce ──────────────────────────────────────────


class TestGitStatusServiceDebounce:
    def test_rapid_refresh_calls_collapse_into_one_run(self, qapp, monkeypatch, tmp_path) -> None:
        calls: list[int] = []
        monkeypatch.setattr(GitStatusService, "_run", lambda self: calls.append(1))

        svc = GitStatusService(tmp_path, debounce_ms=30)
        try:
            svc.request_refresh()
            svc.request_refresh()
            svc.request_refresh()
            # Bounded pump, not a fixed qWait(150): on a loaded CI runner a
            # 30 ms single-shot timer can land later than that (ubuntu flake,
            # same shape as test_file_watch_service's debounce fix).
            assert _wait_until(lambda: len(calls) >= 1)
            QTest.qWait(60)  # a second run would have landed by now

            assert calls == [1]
        finally:
            timer = getattr(svc, "_timer", None)
            if timer is not None:
                timer.stop()

    def test_no_refresh_before_debounce_window_elapses(self, qapp, monkeypatch, tmp_path) -> None:
        calls: list[int] = []
        monkeypatch.setattr(GitStatusService, "_run", lambda self: calls.append(1))

        svc = GitStatusService(tmp_path, debounce_ms=500)
        svc.request_refresh()
        QTest.qWait(50)

        assert calls == []


# ── ProjectFileIndex: diagnostics (#365 phase 10) ────────────────────────


def _wait_until(predicate, timeout_ms: int = 3000, step_ms: int = 25) -> bool:
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        QTest.qWait(step_ms)
        elapsed += step_ms
    return predicate()


class TestProjectFileIndexDiagnostics:
    def test_initial_diagnostics_are_empty(self, qapp, tmp_path: Path) -> None:
        index = ProjectFileIndex([tmp_path])

        diag = index.diagnostics()

        assert diag["last_scan_dir"] is None
        assert diag["last_scan_ms"] is None
        assert diag["last_scan_entry_count"] == 0
        assert diag["scan_count"] == 0

    def test_request_list_records_timing_and_entry_count(self, qapp, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.py").write_text("x", encoding="utf-8")
        (root / "b.py").write_text("y", encoding="utf-8")
        index = ProjectFileIndex([root])

        index.request_list(root)
        assert _wait_until(lambda: index.diagnostics()["scan_count"] == 1)

        diag = index.diagnostics()
        assert diag["last_scan_dir"] == str(root.resolve())
        assert diag["last_scan_ms"] is not None
        assert diag["last_scan_ms"] >= 0.0
        assert diag["last_scan_entry_count"] == 2

    def test_repeated_scans_increment_scan_count(self, qapp, tmp_path: Path) -> None:
        index = ProjectFileIndex([tmp_path])

        index.request_list(tmp_path)
        assert _wait_until(lambda: index.diagnostics()["scan_count"] == 1)
        index.request_list(tmp_path)
        assert _wait_until(lambda: index.diagnostics()["scan_count"] == 2)
