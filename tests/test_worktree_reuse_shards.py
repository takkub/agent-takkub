"""#960: `assign --shards N --isolation worktree` on a card that already had a
worktree recorded handed every shard pane shard 1's checkout — the reuse
registry was keyed by (card, base role), so frontend#2 found frontend#1's."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_takkub import worktree_reuse
from agent_takkub.worktree_manager import WorktreeInfo


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def wt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorktreeInfo:
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=repo,
    )
    base = _git("rev-parse", "HEAD", cwd=repo)
    dest = tmp_path / "wt" / "frontend-1-1"
    _git("worktree", "add", "-q", str(dest), "-b", "wt/frontend-1-1", base, cwd=repo)
    return WorktreeInfo(path=str(dest), branch="wt/frontend-1-1", base_sha=base, git_root=str(repo))


def test_shard_two_does_not_get_shard_one_worktree(wt: WorktreeInfo) -> None:
    worktree_reuse.record("p", "card1", "frontend#1", wt)
    assert worktree_reuse.find("p", "card1", "frontend#1") is not None
    assert worktree_reuse.find("p", "card1", "frontend#2") is None


def test_worktree_held_by_another_pane_is_never_reused(wt: WorktreeInfo) -> None:
    worktree_reuse.record("p", "card1", "frontend", wt)
    held = {os.path.normcase(os.path.normpath(wt.path))}
    assert worktree_reuse.find("p", "card1", "frontend", exclude_paths=held) is None
