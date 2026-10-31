"""#961: `wt/*` branches whose worktree checkout is gone stayed in the user's
repo forever (7 found in one project) — `clean_isolated` only walks
`git worktree list`, so it never saw them."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_takkub.worktree_manager import WorktreeManager


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


def test_merged_dangling_wt_branch_removed_unmerged_kept(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "branch", "wt/qa-1")  # merged: same tip as main
    _git(repo, "checkout", "-q", "-b", "wt/frontend-2")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged work")
    _git(repo, "checkout", "-q", "main")
    held = tmp_path / "held"
    _git(repo, "worktree", "add", "-q", str(held), "-b", "wt/backend-3")
    _git(repo, "branch", "feature/mine")  # not ours — never touched

    lines = WorktreeManager().clean_dangling_branches(str(repo))

    assert _branches(repo) == {"main", "wt/frontend-2", "wt/backend-3", "feature/mine"}
    assert any(line.startswith("REMOVED wt/qa-1") for line in lines), lines
    assert any(line.startswith("KEEP  wt/frontend-2") for line in lines), lines

    WorktreeManager().clean_dangling_branches(str(repo), force=True)
    assert _branches(repo) == {"main", "wt/backend-3", "feature/mine"}
