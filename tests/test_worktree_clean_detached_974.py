"""#974 — `takkub worktree clean` sweeps DETACHED checkouts under the managed
root (a pane's `git worktree add --detach` verify sibling).

Live incident (saas_admin_amb, 2.2.42): two detached verify worktrees, both
merged and clean, held 2.4 GB; `clean` said "nothing to clean" (only `wt/*`
branches were listed) while the guard forbids a raw `git worktree remove`.
Real git so the ancestry/cleanliness rules are proven on an actual repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from agent_takkub import worktree_manager as wm

    monkeypatch.setattr(wm, "DATA_HOME", tmp_path / "data-home")
    # Age gate is not under test here — every worktree counts as old.
    monkeypatch.setattr(wm, "worktree_created_ts", lambda _b, _p: 1_000_000_000.0)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    root = wm.worktrees_managed_root() / "proj"
    root.mkdir(parents=True)
    return wm, repo, git, root


def _add_detached(git, root: Path, name: str) -> Path:
    path = root / name
    git("worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def test_merged_clean_detached_is_listed_and_removed(env):
    wm, repo, git, root = env
    path = _add_detached(git, root, "backend-verify-1793680829")
    mgr = wm.WorktreeManager()
    rows = mgr.list_isolated(str(repo))
    assert [r["detached"] for r in rows] == [True]
    assert rows[0]["branch"].startswith("(detached ")

    lines = mgr.clean_isolated(str(repo))

    assert len(lines) == 1 and lines[0].startswith("REMOVED worktree"), lines
    assert not path.exists()
    assert "backend-verify" not in git("worktree", "list")
    assert git("branch", "--list") == "* main"  # no branch touched


def test_detached_with_unmerged_commit_is_kept_even_with_force(env):
    wm, repo, git, root = env
    path = _add_detached(git, root, "frontend-1793680830")
    (path / "b.txt").write_text("b\n", encoding="utf-8")
    git("add", "b.txt", cwd=path)
    git("commit", "-q", "-m", "work", cwd=path)

    lines = wm.WorktreeManager().clean_isolated(str(repo), force=True)

    assert len(lines) == 1 and lines[0].startswith("KEEP") and "detached" in lines[0], lines
    assert path.exists()


def test_dirty_detached_is_kept(env):
    wm, repo, git, root = env
    path = _add_detached(git, root, "qa-verify-1793680831")
    (path / "a.txt").write_text("changed\n", encoding="utf-8")

    lines = wm.WorktreeManager().clean_isolated(str(repo), force=True)

    assert len(lines) == 1 and lines[0].startswith("KEEP"), lines
    assert path.exists()


def test_detached_outside_managed_root_is_ignored(env, tmp_path):
    wm, repo, git, _root = env
    outside = tmp_path / "scratch" / "user-verify"
    git("worktree", "add", "-q", "--detach", str(outside), "HEAD")

    mgr = wm.WorktreeManager()
    assert mgr.list_isolated(str(repo)) == []
    assert mgr.clean_isolated(str(repo), force=True) == []
    assert outside.exists()


def test_detached_inside_grace_window_is_kept(env, monkeypatch):
    import time

    wm, repo, git, root = env
    monkeypatch.setattr(wm, "worktree_created_ts", lambda _b, _p: time.time() - 600)
    path = _add_detached(git, root, "backend-verify-1793680999")

    lines = wm.WorktreeManager().clean_isolated(str(repo))

    assert len(lines) == 1 and lines[0].startswith("KEEP"), lines
    assert path.exists()
