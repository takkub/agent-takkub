"""Orchestrator wiring for per-pane worktree isolation (issue #81, Phase 1).

Covers the assign→dispatch substitution, the git-repo fallback, and the
done/close finalize (merge proposal vs safe-remove vs keep-dirty). The
WorktreeManager is faked so nothing touches a real repo.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub import worktree_manager as wm_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.worktree_manager import WorktreeInfo


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    return app or QCoreApplication([])


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    # Capture Lead notices instead of driving a real pane.
    o._notify_lead = MagicMock()  # type: ignore[assignment]
    return o


class _FakeMgr:
    """Fake WorktreeManager with scripted lifecycle results."""

    def __init__(
        self,
        info: WorktreeInfo | None = None,
        reason: str = "",
        commits: int = 0,
        dirty: bool = False,
        remove_ok: bool = True,
        remove_reason: str = "",
    ):
        self._info = info
        self._reason = reason
        self._commits = commits
        self._dirty = dirty
        self._remove = (remove_ok, remove_reason)
        self.safe_remove_calls = 0

    def create(self, base_cwd, project_ns, role, ts, exclude_ports=frozenset()):
        self.last_exclude_ports = set(exclude_ports)
        return self._info, self._reason

    def commit_count(self, info):
        return self._commits

    def is_dirty(self, info):
        return self._dirty

    def diffstat(self, info):
        return " src/x.ts | 3 +++"

    def safe_remove(self, info):
        self.safe_remove_calls += 1
        return self._remove


def _info() -> WorktreeInfo:
    return WorktreeInfo(
        path="/wt/frontend-1", branch="wt/frontend-1", base_sha="b", git_root="/repo"
    )


# ── assign → worktree dispatch ──────────────────────────────────────────────


class TestAssignWithWorktree:
    def test_success_dispatches_into_worktree(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info())
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]
        orch._tag_pane_worktree = MagicMock()  # type: ignore[assignment]

        ok, _ = orch._assign_with_worktree(
            "frontend", "/repo/web", "build X", False, False, 0, False, "proj"
        )
        assert ok
        # dispatched with the worktree checkout as cwd + the worktree dict
        _, kwargs = orch._assign_dispatch.call_args
        args = orch._assign_dispatch.call_args[0]
        assert args[1] == "/wt/frontend-1"  # cwd substituted
        assert kwargs["worktree"] == _info().as_dict()
        # commit-on-your-branch hint appended (e2e finding: role policy told the
        # pane not to commit, so finalize could never propose a merge)
        assert "workspace isolation" in args[2]
        assert "wt/frontend-1" in args[2]
        # Lead told it's isolated; pane tagged with the branch chip
        assert orch._notify_lead.called
        orch._tag_pane_worktree.assert_called_once_with("proj", "frontend", "wt/frontend-1")

    def test_fallback_when_not_git_repo(self, orch, monkeypatch):
        fake = _FakeMgr(info=None, reason="ไม่ใช่ git repo — ใช้ shared cwd แทน")
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]

        orch._assign_with_worktree(
            "backend", "/repo/api", "build Y", False, False, 0, False, "proj"
        )
        args = orch._assign_dispatch.call_args[0]
        kwargs = orch._assign_dispatch.call_args[1]
        assert args[1] == "/repo/api"  # ORIGINAL cwd, not a worktree
        assert kwargs["worktree"] is None
        # Lead warned about the fallback
        assert orch._notify_lead.called
        warn = orch._notify_lead.call_args[0][1]
        assert "shared cwd" in warn

    def test_fallback_when_no_cwd(self, orch, monkeypatch):
        monkeypatch.setattr(orch_mod, "default_cwd_for_role", lambda *a, **k: None)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]

        orch._assign_with_worktree("qa", None, "t", False, False, 0, False, "proj")
        kwargs = orch._assign_dispatch.call_args[1]
        assert kwargs["worktree"] is None  # degraded to shared
        assert orch._notify_lead.called


# ── done/close finalize ─────────────────────────────────────────────────────


class TestFinalizeWorktree:
    def test_commits_produce_merge_proposal(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info(), commits=3)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "frontend", _info().as_dict())
        assert fake.safe_remove_calls == 0  # never removed — Lead merges first
        msg = orch._notify_lead.call_args[0][1]
        assert "merge --no-ff wt/frontend-1" in msg
        assert "3 commit" in msg

    def test_empty_clean_worktree_is_safe_removed(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info(), commits=0, remove_ok=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict())
        assert fake.safe_remove_calls == 1
        # a clean empty removal is silent (no merge proposal / keep warning)
        assert not orch._notify_lead.called

    def test_dirty_worktree_kept_and_warns(self, orch, monkeypatch):
        fake = _FakeMgr(
            info=_info(), commits=0, remove_ok=False, remove_reason="uncommitted changes"
        )
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict())
        assert fake.safe_remove_calls == 1
        warn = orch._notify_lead.call_args[0][1]
        assert "เก็บไว้" in warn  # kept, not lost
        assert "uncommitted" in warn

    def test_finalize_never_raises(self, orch, monkeypatch):
        # A malformed worktree dict must not break done()/close().
        orch._finalize_worktree("proj", "qa", {"bogus": True})
        # no exception; nothing proposed
        assert not orch._notify_lead.called


class TestWorktreeHint:
    def test_hint_appended_with_branch(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        out = _append_worktree_hint("build X", "wt/frontend-9")
        assert out.startswith("build X")
        assert "workspace isolation" in out
        assert "wt/frontend-9" in out
        assert "commit" in out

    def test_hint_idempotent_on_replay(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        once = _append_worktree_hint("build X", "wt/x-1")
        twice = _append_worktree_hint(once, "wt/x-1")
        assert twice == once

    def test_hint_carries_post_create_commands(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        out = _append_worktree_hint("build X", "wt/x-1", ("pnpm install", "pnpm build"))
        assert "pnpm install" in out and "pnpm build" in out
        assert out.index("pnpm install") < out.index("pnpm build")  # order preserved

    def test_hint_without_post_create_has_no_setup_block(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        out = _append_worktree_hint("build X", "wt/x-1")
        assert "ก่อนเริ่มงาน" not in out

    def test_hint_carries_dev_port(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        out = _append_worktree_hint("build X", "wt/x-1", (), 5311)
        assert "port 5311" in out and "PORT=5311" in out

    def test_hint_no_port_line_when_zero(self):
        from agent_takkub.orchestrator_text import _append_worktree_hint

        assert "dev server" not in _append_worktree_hint("build X", "wt/x-1")


class TestRequestRestart:
    """`takkub restart` → Orchestrator.request_restart() → deferred signal →
    main_window._restart_cockpit (persist + relaunch)."""

    def test_replies_ok_and_emits_deferred(self, orch, qapp):
        fired: list[bool] = []
        orch.restartRequested.connect(lambda: fired.append(True))

        ok, msg = orch.request_restart()
        assert ok
        assert "restart" in msg.lower()
        # Deferred: NOT emitted synchronously — the IPC reply must flush first.
        assert fired == []
        # Fires on the event loop after the 200 ms timer.
        import time as _time

        deadline = _time.monotonic() + 2.0
        while not fired and _time.monotonic() < deadline:
            qapp.processEvents()
        assert fired == [True]
