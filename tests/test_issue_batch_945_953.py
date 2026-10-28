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


def test_reassign_during_close_grace_replaces_the_forwarded_task() -> None:
    """#954: close a pane holding an undelivered task, then assign the same
    work on claude — the new pane got the forwarded task AND the new one."""
    from agent_takkub import orchestrator as orch_mod

    ps = PaneState()
    ps.last_assigned_task = "new assign text"  # the Lead re-assigned in the grace
    orch = MagicMock()
    orch._project_panes.return_value = {}
    orch._pane_state = {"p::backend": ps}
    Orchestrator._resume_after_close(orch, "p", "backend", ps, "/cwd", "old task text")
    orch.spawn.assert_not_called()
    assert orch_mod._CLOSE_FORWARD_GRACE_MS >= 5_000

    ps.last_assigned_task = "old task text"  # nobody re-assigned: forward as before
    orch.spawn.return_value = (True, "ok")
    Orchestrator._resume_after_close(orch, "p", "backend", ps, "/cwd", "old task text")
    orch.spawn.assert_called_once()


def test_post_compact_status_has_no_raw_terminal_bytes(tmp_path: Path) -> None:
    """#956: the brief pasted '[44;3H[?25h…]0;⠦ ALPR' into Lead's prompt."""
    from agent_takkub.orchestrator import _post_compact_screen_tail

    log = tmp_path / "t.log"
    log.write_bytes(
        b"\x1b[44;3H\x1b[?25h\x1b[?2026l\x1b]0;\xe2\xa0\xa6 ALPR\x07\x1b[39m Running tests 12/40\r\n"
    )
    pane = MagicMock()
    pane.session.display_lines.return_value = []
    pane._transcript_path = str(log)
    assert _post_compact_screen_tail(pane) == ["Running tests 12/40"]
    pane.session.display_lines.return_value = ["", "  > writing src/x.ts", "─────"]
    assert _post_compact_screen_tail(pane) == ["> writing src/x.ts"]


def test_substitution_note_never_says_claude_replaces_claude(monkeypatch) -> None:
    """#955: 'ปิดโดย user (toggle): claude → Claude รับแทน' contradicted itself."""
    from agent_takkub import lead_context, provider_state

    monkeypatch.setattr(provider_state, "is_disabled", lambda p: p == "claude")
    text = lead_context._build_lead_context_text("agent-takkub") or ""
    assert "ปิดโดย user (toggle):** claude" not in text
