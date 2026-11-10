"""#995: Lead assigned `--cwd <a detached linked worktree it made>` to a live
pane (no --isolation). The pane's session cwd stayed the main tree, so the
digest said "branch:master · merge:N/A (shared tree)" with the main tree's
dirt. Done's git facts must read the worktree and name it + its HEAD."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_takkub.worktree_manager import WorktreeManager


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_done_facts_name_the_linked_worktree_and_its_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@example.test", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    (repo / "main-dirt.txt").write_text("not this task's", encoding="utf-8")
    wt = tmp_path / "devops-integ-r4"
    _git("worktree", "add", "--detach", str(wt), cwd=repo)

    mgr = WorktreeManager()
    base_sha, git_root, _dirty = mgr.shared_tree_baseline(str(wt))
    assert git_root and Path(git_root).resolve() == wt.resolve()
    (wt / "b.txt").write_text("task work", encoding="utf-8")

    # done(): the pane's session cwd is still the MAIN tree.
    facts = mgr.collect_done_git_facts(
        worktree=None, pane_cwd=str(repo), assign_base_sha=base_sha, assign_git_root=git_root
    )
    assert facts["kind"] == "shared"
    assert Path(facts["linked_worktree"]).resolve() == wt.resolve()
    assert facts["linked_head"] == base_sha[:8]
    assert facts["branch"] is None  # detached — never "master"
    assert "b.txt" in facts["porcelain"] and "main-dirt.txt" not in facts["porcelain"]


def test_plain_repo_is_not_reported_as_a_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.test", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    mgr = WorktreeManager()
    base_sha, git_root, _ = mgr.shared_tree_baseline(str(repo))
    facts = mgr.collect_done_git_facts(
        worktree=None, pane_cwd=str(repo), assign_base_sha=base_sha, assign_git_root=git_root
    )
    assert "linked_worktree" not in facts
    assert facts["branch"] == "main"
