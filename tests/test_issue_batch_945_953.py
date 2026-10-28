"""Regressions for the 2026-10-28 batch (#945 #947) — the rest live next to
the code they cover (backlog, digest regex, worktree, quota policy, stall
classifier)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.spawn_engine import PaneState, _linked_worktree_common_dir


def test_fresh_assign_drops_the_previous_providers_quota_stall() -> None:
    """#945: qa hit gemini quota (resets 50h45m), Lead closed it and assigned
    the work on claude — 30 s later the claude pane was listed stalled:quota
    and parked again, because the old provider's stall rode on the slot."""
    ps = PaneState()
    ps.quota_provider = "gemini"
    ps.rate_limited_until = time.time() + 50 * 3600
    ps.quota_marker = "individual quota reached"
    ps.limit_confirm_first_attempt_ts = time.time()
    orch = MagicMock()
    Orchestrator._dispatch_reset_task_state(
        orch,
        MagicMock(),
        MagicMock(),
        False,
        "qa",
        None,
        None,
        "claude",
        None,
        "task",
        "p",
        "p",
        "claude",
        ps,
        False,
        "qa",
        "qa",
        {"path": "/wt", "branch": "wt/qa-1"},
    )
    assert ps.rate_limited_until == 0.0
    assert ps.quota_marker == ""
    assert ps.limit_confirm_first_attempt_ts == 0.0


def test_codex_sandbox_gets_the_main_repo_git_dir_in_a_worktree(tmp_path: Path) -> None:
    """#947: a codex pane in `--isolation worktree` could not `git commit` —
    the index/objects live in <main>/.git, outside its writable roots."""

    def git(*a: str, cwd: Path) -> None:
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "b",
        cwd=main,
    )
    git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt/x", cwd=main)

    found = _linked_worktree_common_dir(str(tmp_path / "wt"))
    assert found is not None and Path(found).resolve() == (main / ".git").resolve()
    assert _linked_worktree_common_dir(str(main)) is None  # a normal checkout needs nothing
    assert _linked_worktree_common_dir(None) is None
