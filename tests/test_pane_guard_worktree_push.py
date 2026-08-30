"""#438 — pane guard and `--isolation worktree`:

* case 1: a worktree pane may push ITS OWN `wt/<role>-<ts>` branch, named
  explicitly, no force/delete — so it can get CI green before `takkub done`
  without a Lead round-trip. Every other push shape stays Lead-only.
* case 2: worktree-ness is judged from where the git command RUNS — the
  hook cwd OR an explicit `git -C <wt>` / `cd <wt> &&` in the command —
  not from the spawn flag, so a pane working inside its old checkout after
  a flag-less respawn is treated as the worktree pane it is.
"""

from __future__ import annotations

import pytest

from agent_takkub import pane_guard

_WT = r"C:\Users\dev\.agent-takkub\worktrees\tunnel\backend-1787986742"
_WT_POSIX = "/home/dev/.agent-takkub/worktrees/tunnel/backend-1787986742"
_SHARED = r"C:\Users\dev\WebstormProjects\tunnel"


class TestOwnBranchPushAllowed:
    @pytest.mark.parametrize("cwd", [_WT, _WT_POSIX])
    @pytest.mark.parametrize(
        "command",
        [
            "git push -u origin wt/backend-1787986742",
            "git push origin wt/backend-1787986742",
            "git push --set-upstream origin wt/backend-1787986742",
            "git -C . push -u origin wt/backend-1787986742",
        ],
    )
    def test_named_own_branch_from_worktree_cwd(self, command: str, cwd: str) -> None:
        assert pane_guard.classify(command, "backend", cwd=cwd).allowed

    def test_shard_role_slug_matches_its_branch(self) -> None:
        cmd = "git push -u origin wt/backend-3-1787986742"
        assert pane_guard.classify(cmd, "backend#3", cwd=_WT).allowed

    def test_explicit_worktree_path_in_command_counts_even_from_shared_cwd(self) -> None:
        # case 2: cwd is the shared tree but the command names the worktree
        cmd = f"git -C {_WT} push -u origin wt/backend-1787986742"
        assert pane_guard.classify(cmd, "backend", cwd=_SHARED).allowed


class TestPushStillDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "git push",  # unnamed HEAD — guard can't see what it is
            "git push origin",
            "git push origin main",
            "git push -u origin wt/frontend-1787986742",  # someone else's branch
            "git push --force origin wt/backend-1787986742",
            "git push -f origin wt/backend-1787986742",
            "git push --force-with-lease origin wt/backend-1787986742",
            "git push origin --delete wt/backend-1787986742",
            "git push origin +wt/backend-1787986742",
            "git push origin wt/backend-1787986742:main",
            "git push origin wt/backend-1787986742 main",
            "git push --all origin",
            "git push --tags origin wt/backend-1787986742",
        ],
    )
    def test_from_worktree_cwd(self, command: str) -> None:
        v = pane_guard.classify(command, "backend", cwd=_WT)
        assert not v.allowed, command
        assert v.rule == "git_lead_only:push"

    def test_own_branch_from_shared_tree_still_denied(self) -> None:
        v = pane_guard.classify("git push -u origin wt/backend-1787986742", "backend", cwd=_SHARED)
        assert not v.allowed and v.rule == "git_lead_only:push"

    def test_chained_second_push_must_also_qualify(self) -> None:
        cmd = "git push -u origin wt/backend-1787986742 && git push origin main"
        assert not pane_guard.classify(cmd, "backend", cwd=_WT).allowed

    def test_rule_text_names_the_carve_out(self) -> None:
        assert "wt/<role>-*" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT
        assert "#438" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT


class TestWorktreeByCommandTarget:
    """case 2 — commit/merge carve-outs follow the command's own `-C`/`cd`."""

    @pytest.mark.parametrize(
        "command",
        [
            f"git -C {_WT} merge main",
            f'git -C "{_WT}" commit -m "x"',
            f"cd {_WT} && git merge main",
            f"cd {_WT_POSIX}; git commit -am x",
        ],
    )
    def test_commit_merge_allowed_when_command_targets_worktree(self, command: str) -> None:
        assert pane_guard.classify(command, "frontend", cwd=_SHARED).allowed, command

    @pytest.mark.parametrize(
        "command",
        [
            "git merge main",
            f"cd {_SHARED} && git merge main",
            "cd /tmp/worktrees && git commit -m x",  # dir literally named worktrees, no checkout
            f"echo {_WT} && git merge main",  # mentioned, not targeted
        ],
    )
    def test_still_denied_when_not_targeting_a_worktree(self, command: str) -> None:
        assert not pane_guard.classify(command, "frontend", cwd=_SHARED).allowed, command

    def test_targets_worktree_helper(self) -> None:
        assert pane_guard._command_targets_worktree(f"git -C {_WT} status")
        assert pane_guard._command_targets_worktree(f"cd {_WT_POSIX} && ls")
        assert not pane_guard._command_targets_worktree("git status")
