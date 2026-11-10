"""#972 — a relative `cd`/`-C` after `cd <own worktree>` is relative to that
worktree, not to the pane's shared-tree session cwd.

Live incident (saas_admin_amb, 2.2.42): a `--isolation worktree` backend ran
`cd <wt> && git switch … && cd apps/api && eslint --fix … && git commit` and
was denied `git_lead_only:commit` because `apps/api` was resolved against the
shared-tree cwd. Escapes out of the worktree must still be denied.
"""

from __future__ import annotations

import pytest

from agent_takkub import pane_guard

_WT = "C:/Users/dev/.agent-takkub/worktrees/proj/backend-1790996033"
_WT_POSIX = "/c/Users/dev/.agent-takkub/worktrees/proj/backend-1790996033"
_SHARED = "C:/Users/dev/WebstormProjects/proj"


@pytest.mark.parametrize("wt", [_WT, _WT_POSIX])
@pytest.mark.parametrize(
    "tail",
    [
        "git add -A && cd apps/api && node x.js --fix && git commit -m x",
        "cd apps/api && git -C ../.. commit -m x",
        "cd apps && cd api && git commit -m x",
    ],
)
def test_relative_cd_inside_own_worktree_allows_commit(wt: str, tail: str) -> None:
    cmd = f"cd {wt} && {tail}"
    assert pane_guard.classify(cmd, "backend", cwd=_SHARED).allowed


@pytest.mark.parametrize(
    "tail",
    [
        "cd .. && git commit -m x",
        "cd ../frontend-123 && git commit -m x",
        "git -C ../../x commit -m x",
        "cd apps && cd ../../.. && git commit -m x",
        "cd ~ && git commit -m x",
        "cd ~ && cd proj && git commit -m x",
        f"cd {_SHARED} && git commit -m x",
    ],
)
def test_relative_escape_out_of_worktree_still_denied(tail: str) -> None:
    cmd = f"cd {_WT} && {tail}"
    verdict = pane_guard.classify(cmd, "backend", cwd=_SHARED)
    assert not verdict.allowed
    assert verdict.rule == "git_lead_only:commit"


def test_bare_relative_cd_from_shared_cwd_still_denied() -> None:
    assert not pane_guard.classify("cd apps/api && git commit -m x", "backend", cwd=_SHARED).allowed


def test_word_for_in_commit_message_is_not_a_shell_loop_989() -> None:
    """#989 live (saas_admin_amb, 2.2.51): the message "… audit for business
    rejections …" matched the `for` loop check, switching off cd chaining —
    `cd <wt>/apps/api … ; cd ../.. && git commit` was denied in the pane's
    own worktree. A real loop still disables chaining."""
    msg = "feat: audit\n\n- WITHDRAWAL_REJECTED audit for business rejections"
    cmd = f'cd {_WT}/apps/api && pnpm test 2>&1 | tail -2; cd ../.. && git commit -q -m "{msg}"'
    assert pane_guard.classify(cmd, "backend", cwd=_WT).allowed
    assert pane_guard.classify(cmd, "backend", cwd=_SHARED).allowed
    assert not pane_guard._has_shell_loop(f'git commit -m "{msg}"')
    assert pane_guard._has_shell_loop(f'for f in a b; do git commit -m "{msg}"; done')
