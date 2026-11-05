"""#982 — `takkub worktree clean` with several merged worktrees reported
FAILED "is not a working tree" for all but the first.

Mechanism (reproduced on real git): `clean` deletes every checkout first (in
parallel), then drops metadata row by row. The first row's `git worktree
prune` already clears the metadata of every OTHER deleted checkout, so their
own `git worktree remove` fails with "is not a working tree" — the goal state,
which was reported as FAILED and left the merged branches behind.
Real git so the prune interaction is proven on an actual repo.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from agent_takkub import worktree_manager as wm

    monkeypatch.setattr(wm, "DATA_HOME", tmp_path / "data-home")
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


def test_clean_several_merged_worktrees_all_removed_982(env):
    wm, repo, git, root = env
    names = [f"frontend-{i}-179384{i:04d}" for i in range(4)]
    paths = []
    for name in names:
        path = root / name
        git("worktree", "add", "-q", "-b", f"wt/{name}", str(path), "HEAD")
        paths.append(path)

    lines = wm.WorktreeManager().clean_isolated(str(repo))

    assert all(line.startswith("REMOVED") for line in lines), lines
    assert len(lines) == len(names)
    assert not any(p.exists() for p in paths)
    assert git("for-each-ref", "--format=%(refname:short)", "refs/heads/wt/") == ""
    assert git("worktree", "list", "--porcelain").count("worktree ") == 1


def test_metadata_already_pruned_counts_as_removed_982(env):
    wm, repo, git, root = env
    path = root / "backend-1793841929"
    git("worktree", "add", "-q", "-b", "wt/backend-1793841929", str(path), "HEAD")
    mgr = wm.WorktreeManager()
    import shutil

    shutil.rmtree(path)
    git("worktree", "prune")  # metadata gone before our own remove runs

    ok, detail = mgr._drop_worktree_metadata(str(repo), str(path))

    assert ok and detail == ""


def test_still_registered_worktree_is_reported_982(env):
    wm, repo, _git, _root = env
    mgr = wm.WorktreeManager()
    ok, detail = mgr._drop_worktree_metadata(str(repo), str(repo))  # main tree
    assert not ok and detail
