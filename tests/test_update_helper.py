"""Tests for `update_helper` — the git wrapper behind the cockpit's
self-update button. Mocks subprocess.run so no real `git` calls leak
out of the test runner; the goal is to pin the parsing + the
dirty-tree guard, not to exercise git itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub import update_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess shaped the way the real `_git` helper
    returns it. subprocess.CompletedProcess is the simplest stand-in
    that satisfies attribute access on `returncode`, `stdout`,
    `stderr` without needing a Mock spec."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestIsGitRepo:
    def test_true_when_dot_git_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(update_helper, "REPO_ROOT", tmp_path)
        assert update_helper.is_git_repo() is True

    def test_false_when_dot_git_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Tarball install scenario — no .git folder at the repo root.
        monkeypatch.setattr(update_helper, "REPO_ROOT", tmp_path)
        assert update_helper.is_git_repo() is False


class TestFetchRemote:
    def test_returns_false_when_not_a_git_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: False)
        ok, msg = update_helper.fetch_remote()
        assert ok is False
        assert "not a git repo" in msg

    def test_returns_true_on_zero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        with patch.object(update_helper, "_git", return_value=_proc(0)):
            ok, msg = update_helper.fetch_remote()
        assert ok is True
        assert msg == "fetched"

    def test_surfaces_last_stderr_line_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        stderr = "fatal: unable to access\nssh: connection refused"
        with patch.object(update_helper, "_git", return_value=_proc(1, stderr=stderr)):
            ok, msg = update_helper.fetch_remote()
        assert ok is False
        # Show the most-specific (last) error line, not the generic header.
        assert msg == "ssh: connection refused"

    def test_returns_false_when_git_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        with patch.object(update_helper, "_git", side_effect=FileNotFoundError("git")):
            ok, msg = update_helper.fetch_remote()
        assert ok is False
        assert "git binary" in msg


class TestLocalStatus:
    def test_parses_porcelain_into_dirty_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        porcelain = " M src/foo.py\nA  src/bar.py\n M README.md"
        revlist = "0\t0"

        def fake_git(*args, **kwargs):
            if args[0] == "status":
                return _proc(0, stdout=porcelain)
            return _proc(0, stdout=revlist)

        with patch.object(update_helper, "_git", side_effect=fake_git):
            status = update_helper.local_status()
        assert status["ok"] is True
        assert status["clean"] is False
        assert "src/foo.py" in status["dirty_files"]
        assert "src/bar.py" in status["dirty_files"]
        assert "README.md" in status["dirty_files"]

    def test_skips_untracked_question_mark_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Untracked files ("??") never block a fast-forward pull —
        # they shouldn't pollute the dirty-files warning either.
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        porcelain = "?? scratch.txt\n?? notes.md\n M tracked.py"
        revlist = "0\t0"

        def fake_git(*args, **kwargs):
            if args[0] == "status":
                return _proc(0, stdout=porcelain)
            return _proc(0, stdout=revlist)

        with patch.object(update_helper, "_git", side_effect=fake_git):
            status = update_helper.local_status()
        assert status["dirty_files"] == ["tracked.py"]
        assert "scratch.txt" not in status["dirty_files"]

    def test_parses_ahead_behind_from_revlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)

        def fake_git(*args, **kwargs):
            if args[0] == "status":
                return _proc(0, stdout="")
            return _proc(0, stdout="3\t5")  # 3 ahead, 5 behind

        with patch.object(update_helper, "_git", side_effect=fake_git):
            status = update_helper.local_status()
        assert status["ahead"] == 3
        assert status["behind"] == 5
        assert status["clean"] is True

    def test_returns_error_when_not_a_git_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: False)
        status = update_helper.local_status()
        assert status["ok"] is False
        assert status["clean"] is False
        assert status["dirty_files"] == []


class TestPullUpdates:
    def test_refuses_when_tree_dirty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even though the user could push the button, the helper itself
        # refuses if `local_status` reports dirty files. This guard
        # exists in the helper (not just the UI) so a future CLI
        # or scripted caller can't sidestep it.
        monkeypatch.setattr(
            update_helper,
            "local_status",
            lambda: {
                "ok": True,
                "clean": False,
                "ahead": 0,
                "behind": 1,
                "dirty_files": [".claude/agents/backend.md"],
            },
        )
        ok, msg = update_helper.pull_updates()
        assert ok is False
        assert "local edits present" in msg
        assert ".claude/agents/backend.md" in msg

    def test_short_circuits_when_already_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            update_helper,
            "local_status",
            lambda: {
                "ok": True,
                "clean": True,
                "ahead": 0,
                "behind": 0,
                "dirty_files": [],
            },
        )
        ok, msg = update_helper.pull_updates()
        assert ok is True
        assert "up to date" in msg

    def test_runs_ff_only_pull_when_clean_and_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            update_helper,
            "local_status",
            lambda: {
                "ok": True,
                "clean": True,
                "ahead": 0,
                "behind": 4,
                "dirty_files": [],
            },
        )
        seen_args: list[tuple] = []

        def fake_git(*args, **kwargs):
            seen_args.append(args)
            return _proc(0, stdout="Fast-forward")

        with patch.object(update_helper, "_git", side_effect=fake_git):
            ok, msg = update_helper.pull_updates()
        assert ok is True
        assert "4 commits" in msg
        # pull command used --ff-only and the origin/main pair
        assert seen_args[0][:4] == ("pull", "--ff-only", "origin", "main")

    def test_surfaces_pull_failure_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            update_helper,
            "local_status",
            lambda: {
                "ok": True,
                "clean": True,
                "ahead": 0,
                "behind": 2,
                "dirty_files": [],
            },
        )
        stderr = "header\nfatal: Not possible to fast-forward, aborting."
        with patch.object(update_helper, "_git", return_value=_proc(1, stderr=stderr)):
            ok, msg = update_helper.pull_updates()
        assert ok is False
        # User sees the actual git complaint (last line), not "git pull failed".
        assert "fast-forward" in msg


class TestPyprojectChangedInPull:
    def test_returns_true_when_pyproject_in_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(
            update_helper,
            "_git",
            return_value=_proc(0, stdout="pyproject.toml\nsrc/foo.py\n"),
        ):
            assert update_helper.pyproject_changed_in_pull("a", "b") is True

    def test_returns_false_when_only_other_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(
            update_helper,
            "_git",
            return_value=_proc(0, stdout="src/foo.py\nREADME.md\n"),
        ):
            assert update_helper.pyproject_changed_in_pull("a", "b") is False

    def test_returns_false_when_same_sha(self) -> None:
        # No-op short-circuit: caller passed pre-pull == post-pull
        # because nothing changed. Skip the diff entirely.
        assert update_helper.pyproject_changed_in_pull("abc", "abc") is False

    def test_returns_false_on_git_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(update_helper, "_git", return_value=_proc(128, stderr="bad ref")):
            assert update_helper.pyproject_changed_in_pull("a", "b") is False


class TestCurrentSha:
    def test_returns_empty_when_not_a_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: False)
        assert update_helper.current_sha() == ""

    def test_returns_trimmed_sha_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_helper, "is_git_repo", lambda: True)
        with patch.object(update_helper, "_git", return_value=_proc(0, stdout="abc1234def567\n")):
            assert update_helper.current_sha() == "abc1234def567"
