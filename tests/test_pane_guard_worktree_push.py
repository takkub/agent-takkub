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


class TestGitDashCUppercaseFlagSeenThrough:
    """#466: `-C <path>` is a two-token flag (the value is a separate arg),
    which `_GIT_SUBCMD_GAP` never used to skip over — so `git -C <path>
    <subcommand>` silently bypassed EVERY subcommand-gated rule outright,
    not just push. Confirmed live before the fix: `git -C . reset --hard`
    and `git -C . push origin main --force` were both allowed from a
    worktree cwd — the deny pattern couldn't even see the subcommand past
    the unconsumed path argument, so this wasn't a false-deny, it was a
    silent full bypass. `TestOwnBranchPushAllowed.
    test_named_own_branch_from_worktree_cwd`'s `git -C . push -u origin
    wt/...` case predates this fix and was "passing" for that same wrong
    reason — this class pins that it now passes because the branch is
    actually validated."""

    _WT = r"C:\Users\dev\.agent-takkub\worktrees\tunnel\backend-1787986742"

    @pytest.mark.parametrize(
        "command",
        [
            "git -C . push origin main --force",
            "git -C . push --force origin wt/backend-1787986742",
            "git -C . push -u origin wt/frontend-1787986742",  # someone else's branch
            f"git -C {_WT} push origin +wt/backend-1787986742",
        ],
    )
    def test_bad_push_denied_even_with_dash_c(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend", cwd=self._WT)
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule == "git_lead_only:push"

    @pytest.mark.parametrize(
        "command",
        [
            "git -C . reset --hard HEAD~1",  # no carve-out even inside a worktree
            f"git -C {_WT} branch -D main",
        ],
    )
    def test_other_subcommands_still_gated_with_dash_c(self, command: str) -> None:
        assert not pane_guard.classify(command, "backend", cwd=self._WT).allowed, command

    def test_commit_with_dash_c_still_gated_outside_a_worktree(self) -> None:
        """`commit` DOES carve out inside a worktree (asserted elsewhere) —
        this pins that `-C .` doesn't grant that carve-out on its own when
        neither the cwd nor the command's own `-C` target a worktree."""
        assert not pane_guard.classify(
            "git -C . commit -m x", "backend", cwd=r"C:\Users\dev\proj"
        ).allowed

    def test_good_push_still_allowed_with_dash_c(self) -> None:
        assert pane_guard.classify(
            "git -C . push -u origin wt/backend-1787986742", "backend", cwd=self._WT
        ).allowed

    def test_rtk_prefix_and_dash_c_compose(self) -> None:
        """Both #466 fixes (rtk-prefix and `-C` value) apply together — a
        pane following CLAUDE.md's rtk rule from its old checkout after a
        flag-less respawn (#438 case 2) still gets the real carve-out, and
        still gets denied for a bad push."""
        good = f"rtk git -C {self._WT} push -u origin wt/backend-1787986742"
        bad = f"rtk git -C {self._WT} push origin main --force"
        assert pane_guard.classify(good, "backend", cwd=r"C:\Users\dev\proj").allowed
        assert not pane_guard.classify(bad, "backend", cwd=r"C:\Users\dev\proj").allowed


class TestPushDenyReasonNamesTheCarveOut:
    """#466 point 3: the deny reason for a push inside the pane's own
    worktree must spell out rule + a copy-pasteable passing example with
    THIS pane's own role slug, not just point at the generic Lead-only
    prose every other git_lead_only rule shares."""

    _WT = r"C:\Users\dev\.agent-takkub\worktrees\tunnel\backend-1787986742"

    def test_reason_names_own_expected_branch(self) -> None:
        verdict = pane_guard.classify("git push origin main --force", "backend", cwd=self._WT)
        assert not verdict.allowed
        assert verdict.rule == "git_lead_only:push"
        assert "wt/backend-<ts>" in verdict.reason
        assert "438" in verdict.reason

    def test_head_refspec_form_denied_with_clear_reason(self) -> None:
        """`git push origin HEAD:wt/<branch>` — flagged as suspect in #466's
        own report. Denied on purpose: the guard can't confirm HEAD is
        actually the pane's own branch, only that the refspec text matches
        — so this stays refused with the same actionable message."""
        verdict = pane_guard.classify(
            "git push origin HEAD:wt/backend-1787986742", "backend", cwd=self._WT
        )
        assert not verdict.allowed
        assert verdict.rule == "git_lead_only:push"
        assert "HEAD:" in verdict.reason

    def test_shard_role_gets_its_own_slug_in_reason(self) -> None:
        verdict = pane_guard.classify("git push origin main --force", "backend#3", cwd=self._WT)
        assert not verdict.allowed
        assert "wt/backend-3-<ts>" in verdict.reason

    def test_denied_from_shared_tree_keeps_generic_message(self) -> None:
        """Outside a worktree the carve-out never applies at all — that
        case keeps the existing generic Lead-only text (no per-role branch
        name to offer, since there IS no legitimate push here)."""
        verdict = pane_guard.classify(
            "git push -u origin wt/backend-1787986742", "backend", cwd=r"C:\Users\dev\proj"
        )
        assert not verdict.allowed
        assert "wt/backend-<ts>" not in verdict.reason


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
