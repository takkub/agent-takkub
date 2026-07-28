"""Unit tests for git_status.py — real throwaway git repos via tmp_path.

Exercises the module against actual `git` subprocess calls (no fake runner)
so the porcelain parsing is proven against real output, on both Windows and
macOS CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_takkub import config, git_status


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check
    )


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _commit(path: Path, filename: str, content: str, msg: str) -> None:
    (path / filename).write_text(content, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg)


class TestCollectRepoCleanAndDirty:
    def test_clean_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")

        status = git_status.collect_repo(str(repo), key="k")

        assert status.error is None
        assert status.key == "k"
        assert status.branch == "main"
        assert status.upstream is None
        assert (status.staged, status.modified, status.untracked) == (0, 0, 0)
        assert (status.ahead, status.behind) == (0, 0)
        assert [c.subject for c in status.commits] == ["init"]
        assert status.commits[0].sha
        assert status.commits[0].author == "t"
        assert status.worktrees == []

    def test_dirty_and_untracked_counts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")

        (repo / "a.txt").write_text("changed", encoding="utf-8")  # unstaged modify
        (repo / "b.txt").write_text("new", encoding="utf-8")  # untracked
        (repo / "c.txt").write_text("staged", encoding="utf-8")
        _git(repo, "add", "c.txt")  # staged add

        status = git_status.collect_repo(str(repo))

        assert status.error is None
        assert status.staged == 1
        assert status.modified == 1
        assert status.untracked == 1


class TestAheadBehind:
    def test_ahead_and_behind_via_bare_remote(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        _init_repo(local)
        _commit(local, "a.txt", "1", "init")

        remote = tmp_path / "remote.git"
        _run("git", "init", "-q", "--bare", "-b", "main", str(remote))
        _git(local, "remote", "add", "origin", str(remote))
        _git(local, "push", "-q", "-u", "origin", "main")

        # ahead: a local commit never pushed
        _commit(local, "b.txt", "2", "ahead commit")
        ahead_only = git_status.collect_repo(str(local))
        assert ahead_only.ahead == 1
        assert ahead_only.behind == 0
        assert ahead_only.upstream == "origin/main"

        # behind: another clone pushes, then local fetches (no merge)
        clone2 = tmp_path / "clone2"
        _run("git", "clone", "-q", str(remote), str(clone2))
        _git(clone2, "config", "user.email", "t@t")
        _git(clone2, "config", "user.name", "t")
        _commit(clone2, "c.txt", "3", "remote-only commit")
        _git(clone2, "push", "-q", "origin", "main")
        _git(local, "fetch", "-q", "origin")

        both = git_status.collect_repo(str(local))
        assert both.ahead == 1
        assert both.behind == 1


class TestDetachedHead:
    def test_detached_head_shows_short_sha(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-q", "--detach", sha)

        status = git_status.collect_repo(str(repo))

        assert status.error is None
        assert status.branch.startswith("(detached @ ")
        assert status.branch.endswith(")")


class TestNotARepo:
    def test_non_git_path_returns_error_not_raise(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        status = git_status.collect_repo(str(plain))

        assert status.error is not None
        assert status.branch == ""
        assert status.commits == []
        assert status.worktrees == []

    def test_missing_path_returns_error_not_raise(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does-not-exist")

        status = git_status.collect_repo(missing)

        assert status.error is not None


class TestProjectRepoPathsDedupe:
    def test_dedupes_two_keys_pointing_at_same_repo(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")
        sub = repo / "api"
        sub.mkdir()

        projects = {
            "active": "proj",
            "projects": {"proj": {"paths": {"root": str(repo), "api": str(sub)}}},
        }
        monkeypatch.setattr(config, "load_projects", lambda: projects)

        pairs = git_status.project_repo_paths("proj")

        assert len(pairs) == 1
        key, path = pairs[0]
        assert key == "root"  # closest match to the toplevel wins
        assert Path(path).resolve() == repo.resolve()

    def test_distinct_repos_stay_separate(self, tmp_path: Path, monkeypatch) -> None:
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        _init_repo(repo_a)
        _commit(repo_a, "f.txt", "1", "init")
        _init_repo(repo_b)
        _commit(repo_b, "f.txt", "1", "init")

        projects = {
            "active": "proj",
            "projects": {"proj": {"paths": {"a": str(repo_a), "b": str(repo_b)}}},
        }
        monkeypatch.setattr(config, "load_projects", lambda: projects)

        pairs = git_status.project_repo_paths("proj")

        assert {k for k, _ in pairs} == {"a", "b"}


class TestCollectWiring:
    def test_collect_uses_project_repo_paths(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")

        projects = {"active": "proj", "projects": {"proj": {"paths": {"root": str(repo)}}}}
        monkeypatch.setattr(config, "load_projects", lambda: projects)

        rows = git_status.collect("proj")

        assert len(rows) == 1
        assert rows[0].key == "root"
        assert rows[0].error is None


class TestWorktrees:
    def test_only_wt_prefixed_worktrees_are_reported(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "1", "init")

        wt_dest = tmp_path / "wt-checkout"
        _git(repo, "worktree", "add", "-q", "-b", "wt/test-1", str(wt_dest))
        (wt_dest / "dirty.txt").write_text("uncommitted", encoding="utf-8")

        other_dest = tmp_path / "other-checkout"
        _git(repo, "worktree", "add", "-q", "-b", "feature/other", str(other_dest))

        status = git_status.collect_repo(str(repo))

        branches = {w.branch for w in status.worktrees}
        assert branches == {"wt/test-1"}
        wt = next(w for w in status.worktrees if w.branch == "wt/test-1")
        assert wt.dirty is True
        assert wt.commits_ahead == 0
        assert Path(wt.path).resolve() == wt_dest.resolve()
