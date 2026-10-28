"""#958/#959: cockpit skill links must never let git delete the central store,
and an isolated worktree pane must be able to sync its own base."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub import pane_guard, skill_scan
from agent_takkub.worktree_manager import (
    WorktreeInfo,
    WorktreeManager,
    _make_link,
    ensure_git_excluded,
)

_WT = r"C:\Users\dev\.agent-takkub\worktrees\tunnel\backend-1787986742"
_SHARED = r"C:\Users\dev\WebstormProjects\tunnel"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt/backend-1")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    return repo, wt


def _store_skill(root: Path, name: str = "skill-a") -> Path:
    skill = root / name
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\nbody\n", encoding="utf-8")
    (skill / "references" / "r.md").write_text("r", encoding="utf-8")
    return skill


class TestGitExclude958:
    def test_link_point_in_a_worktree_is_invisible_to_git(self, tmp_path: Path) -> None:
        repo, wt = _repo_with_worktree(tmp_path)
        store = _store_skill(tmp_path / "store")
        link = wt / "sub" / ".claude" / "skills" / "skill-a"
        assert _make_link(store, link) is None

        ensure_git_excluded([link])
        ensure_git_excluded([link])  # idempotent

        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert exclude.count("/sub/.claude/skills/skill-a\n") == 1
        assert _git(wt, "status", "--porcelain", "-uall") == ""
        # the #958 sequence: snapshot everything, then reset it away
        _git(wt, "add", "-A")
        assert _git(wt, "diff", "--cached", "--name-only") == ""
        assert (store / "SKILL.md").is_file()

    def test_outside_git_is_a_no_op(self, tmp_path: Path) -> None:
        ensure_git_excluded([tmp_path / "x" / ".claude" / "skills" / "a"])


class TestWorktreeSkillCopies958:
    @pytest.fixture
    def env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        project_store = tmp_path / "project-skills"
        global_store = tmp_path / "global"
        monkeypatch.setattr(config_mod, "project_skills_dir", lambda ns: project_store / ns)
        monkeypatch.setattr(config_mod, "global_skills_dir", lambda: global_store)
        monkeypatch.setattr(config_mod, "PROJECT_SKILLS_HOME", project_store)
        monkeypatch.setattr(config_mod, "project_skill_excludes", lambda ns: set())
        skill = _store_skill(global_store)
        _repo, wt = _repo_with_worktree(tmp_path)
        return skill, wt

    def test_worktree_gets_a_marked_copy_not_a_link(self, env) -> None:
        from agent_takkub.native_skills import _is_link

        _skill, wt = env
        assert skill_scan.ensure_project_skill_links(wt, "p", copy=True) == []
        dst = wt / ".claude" / "skills" / "skill-a"
        assert not _is_link(dst)
        assert (dst / "references" / "r.md").is_file()
        assert (dst / ".takkub-copy").is_file()
        assert _git(wt, "status", "--porcelain", "-uall") == ""

    def test_git_deleting_the_worktree_never_reaches_the_store(self, env, tmp_path) -> None:
        skill, wt = env
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        _git(tmp_path / "repo", "worktree", "remove", "--force", str(wt))
        assert (skill / "SKILL.md").is_file()
        assert (skill / "references" / "r.md").is_file()

    def test_copy_refreshes_and_is_pruned_with_the_store(self, env) -> None:
        import shutil

        skill, wt = env
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        (skill / "SKILL.md").write_text("---\nname: x\ndescription: v2\n---\n", encoding="utf-8")
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        dst = wt / ".claude" / "skills" / "skill-a"
        assert "v2" in (dst / "SKILL.md").read_text(encoding="utf-8")
        shutil.rmtree(skill)
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        assert not dst.exists()

    def test_users_own_same_named_skill_is_left_alone(self, env) -> None:
        _skill, wt = env
        mine = wt / ".claude" / "skills" / "skill-a"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("mine", encoding="utf-8")
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"
        assert not (mine / ".takkub-copy").exists()

    def test_old_junction_is_replaced_by_a_copy(self, env) -> None:
        from agent_takkub.native_skills import _is_link

        skill, wt = env
        dst = wt / ".claude" / "skills" / "skill-a"
        assert _make_link(skill, dst) is None
        skill_scan.ensure_project_skill_links(wt, "p", copy=True)
        assert not _is_link(dst)
        assert (skill / "SKILL.md").is_file()


class TestRawWorktreeRemoveDenied958:
    @pytest.mark.parametrize("role", ["lead", "backend", "codex"])
    @pytest.mark.parametrize(
        "command",
        [
            "git worktree remove C:/x/worktrees/takma/devops-1",
            "rtk proxy git worktree remove --force ../wt",
            "cd /c/repo; for w in a b; do git -C /c/repo worktree remove $w; done",
        ],
    )
    def test_denied_for_every_role(self, role: str, command: str) -> None:
        verdict = pane_guard.classify(command, role, cwd=_SHARED)
        assert not verdict.allowed
        assert verdict.rule == "worktree_remove:use_takkub_worktree"
        assert "takkub worktree" in verdict.reason

    @pytest.mark.parametrize("command", ["git worktree list", "git worktree add ../x -b y"])
    def test_other_worktree_verbs_untouched_for_lead(self, command: str) -> None:
        assert pane_guard.classify(command, "lead", cwd=_SHARED).allowed


class TestCdAfterGitDoesNotMoveIt959:
    def test_trailing_cd_out_of_own_worktree_is_ignored(self) -> None:
        cmd = f"cd {_WT} && git merge --ff-only origin/main 2>&1 | tail -2; cd .. && ls"
        assert pane_guard.classify(cmd, "backend", cwd=_WT).allowed

    def test_cd_before_git_still_decides(self) -> None:
        cmd = f"cd {_WT} && cd .. && git merge --ff-only origin/main"
        assert not pane_guard.classify(cmd, "backend", cwd=_WT).allowed

    def test_loop_keeps_the_conservative_reading(self) -> None:
        cmd = f"cd {_WT} && git merge --ff-only origin/main; for i in 1; do true; cd .. ; done"
        assert not pane_guard.classify(cmd, "backend", cwd=_WT).allowed


class TestWorktreeBase959:
    def test_base_moves_to_upstream_when_local_head_is_behind(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        main = tmp_path / "main"
        _git(tmp_path, "clone", "-q", str(origin), str(main))
        _git(main, "config", "user.email", "t@t")
        _git(main, "config", "user.name", "t")
        (main / "a.txt").write_text("a\n", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-qm", "base")
        _git(main, "push", "-q", "-u", "origin", "HEAD:main")
        old = _git(main, "rev-parse", "HEAD")
        (main / "b.txt").write_text("b\n", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-qm", "newer")
        _git(main, "push", "-q", "origin", "HEAD:main")
        new = _git(main, "rev-parse", "HEAD")
        _git(main, "reset", "-q", "--hard", old)  # the stale shared checkout

        mgr = WorktreeManager()

        assert mgr._upstream_if_strictly_ahead(str(main), old) == new
        # diverged local work is never dropped
        (main / "c.txt").write_text("c\n", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-qm", "local only")
        local = _git(main, "rev-parse", "HEAD")
        assert mgr._upstream_if_strictly_ahead(str(main), local) == local

    def test_done_snapshot_never_commits_cockpit_claude_dir(self, tmp_path: Path) -> None:
        _repo, wt = _repo_with_worktree(tmp_path)
        base = _git(wt, "rev-parse", "HEAD")
        (wt / "app.txt").write_text("work\n", encoding="utf-8")
        skill = wt / "pkg" / ".claude" / "skills" / "learned"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x", encoding="utf-8")
        info = WorktreeInfo(path=str(wt), branch="wt/backend-1", base_sha=base, git_root="")

        assert WorktreeManager().auto_commit_snapshot(info, "backend", "done")

        assert _git(wt, "show", "--name-only", "--format=", "HEAD").splitlines() == ["app.txt"]
