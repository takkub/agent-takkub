"""Orchestrator wiring for per-pane worktree isolation (issue #81, Phase 1).

Covers the assign→dispatch substitution, the git-repo fallback, and the
done/close finalize (merge proposal vs keep-and-warn on zero commits,
whether the tree is clean or dirty — #161: never auto-remove). The
WorktreeManager is faked so nothing touches a real repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, QTimer

from agent_takkub import orchestrator as orch_mod
from agent_takkub import worktree_manager as wm_mod
from agent_takkub.orchestrator import Orchestrator, PaneState
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
        uncommitted: int = 0,
        merge_conflicts: bool | None = False,
        auto_commit_result: bool = False,
        head_sha: str | None = None,
        already_merged: bool | None = False,
    ):
        self._info = info
        self._reason = reason
        self._commits = commits
        self._dirty = dirty
        self._remove = (remove_ok, remove_reason)
        # #536: defaults to the recorded base_sha (no divergence) so
        # existing "virgin worktree" tests are unaffected unless a test
        # explicitly scripts HEAD having moved past it.
        self._head_sha = head_sha if head_sha is not None else (info.base_sha if info else None)
        self._already_merged = already_merged
        self.last_auto_commit_summary: str | None = None
        self._uncommitted = uncommitted
        self._merge_conflicts = merge_conflicts
        self._auto_commit_result = auto_commit_result
        self.safe_remove_calls = 0
        self.auto_commit_calls = 0

    def create(self, base_cwd, project_ns, role, ts, exclude_ports=frozenset()):
        self.last_exclude_ports = set(exclude_ports)
        return self._info, self._reason

    def commit_count(self, info):
        return self._commits

    def is_dirty(self, info):
        return self._dirty

    def uncommitted_count(self, info):
        return self._uncommitted

    # #496: real_* variants filter CRLF-only phantom dirt; these fakes have
    # no phantom to filter, so they mirror the raw is_dirty/uncommitted_count.
    def real_dirty(self, info):
        return self._dirty

    def real_uncommitted_count(self, info):
        return self._uncommitted

    def crlf_phantom(self, info):
        return False

    def merge_conflicts_with_base(self, git_root, branch):
        return self._merge_conflicts

    def merge_conflict_files(self, git_root, branch):
        # mirror the real API (#442): [] clean, [files] conflict, None unknown
        if self._merge_conflicts is None:
            return None
        return ["docker/nginx.conf"] if self._merge_conflicts else []

    def diffstat(self, info):
        return " src/x.ts | 3 +++"

    def auto_commit_snapshot(self, info, role, summary=""):
        # #525: mirrors the real method — a successful snapshot commit moves
        # HEAD, so a subsequent `commit_count`/`real_dirty`/`real_uncommitted_
        # count` read must see the post-commit state, not the pre-commit
        # script the test set up.
        self.auto_commit_calls += 1
        self.last_auto_commit_summary = summary
        if self._auto_commit_result:
            self._commits += 1
            self._dirty = False
            self._uncommitted = 0
        return self._auto_commit_result

    def head_sha(self, path):
        return self._head_sha

    def branch_merged_into_base(self, git_root, branch):
        return self._already_merged

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

        ok, msg = orch._assign_with_worktree(
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
        # #519: no more separate "worktree-spawn" Lead notice — the
        # confirmation rides in the assign ack Lead already reads.
        assert not orch._notify_lead.called
        assert "wt/frontend-1" in msg
        assert "isolated worktree" in msg
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

    def test_fallback_warns_how_many_panes_share_the_cwd(self, orch, monkeypatch):
        """#494 — once a pane degrades to the shared cwd, the Lead needs to
        know how many OTHER panes are already sitting in that same cwd
        (the ones it can actually collide with), not just that isolation
        failed."""
        fake = _FakeMgr(info=None, reason="git worktree add ล้มเหลว — ใช้ shared cwd แทน")
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]

        sibling1 = MagicMock()
        sibling1._session_cwd = "/repo/api"
        sibling2 = MagicMock()
        sibling2._session_cwd = "/repo/api"
        orch._project_panes("proj")["backend#1"] = sibling1
        orch._project_panes("proj")["backend#2"] = sibling2

        orch._assign_with_worktree(
            "backend#3", "/repo/api", "build Y", False, False, 0, False, "proj"
        )
        warn = orch._notify_lead.call_args[0][1]
        assert "อีก 2 pane" in warn

    def test_fallback_when_no_cwd(self, orch, monkeypatch):
        monkeypatch.setattr(orch_mod, "default_cwd_for_role", lambda *a, **k: None)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]

        orch._assign_with_worktree("qa", None, "t", False, False, 0, False, "proj")
        kwargs = orch._assign_dispatch.call_args[1]
        assert kwargs["worktree"] is None  # degraded to shared
        assert orch._notify_lead.called


class TestPreTrustOnWorktreeAssign:
    """#444: `_assign_with_worktree` pre-trusts the shared worktrees root
    before dispatching into a fresh worktree cwd, but only for a
    claude-backed pane (other providers have no `.claude.json`)."""

    def test_claude_pane_pre_trusts_before_dispatch(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info())
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        pretrust = MagicMock()
        monkeypatch.setattr(wm_mod, "pre_trust_worktrees_root", pretrust)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]
        orch._tag_pane_worktree = MagicMock()  # type: ignore[assignment]

        orch._assign_with_worktree(
            "frontend", "/repo/web", "build X", False, False, 0, False, "proj"
        )

        pretrust.assert_called_once_with("proj")

    def test_non_claude_pane_never_pre_trusts(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info())
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        pretrust = MagicMock()
        monkeypatch.setattr(wm_mod, "pre_trust_worktrees_root", pretrust)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]
        orch._tag_pane_worktree = MagicMock()  # type: ignore[assignment]

        orch._assign_with_worktree(
            "backend", "/repo/api", "build Y", False, False, 0, False, "proj"
        )

        pretrust.assert_not_called()

    def test_fallback_path_never_pre_trusts(self, orch, monkeypatch):
        """No worktree ever got created (fallback to shared cwd) — nothing
        to pre-trust."""
        fake = _FakeMgr(info=None, reason="ไม่ใช่ git repo — ใช้ shared cwd แทน")
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        pretrust = MagicMock()
        monkeypatch.setattr(wm_mod, "pre_trust_worktrees_root", pretrust)
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))  # type: ignore[assignment]

        orch._assign_with_worktree(
            "backend", "/repo/api", "build Y", False, False, 0, False, "proj"
        )

        pretrust.assert_not_called()


class TestBareRoleWorktreeCollision:
    """#162: `assign --isolation worktree` fired 3x back-to-back at the same
    bare role name (no `#N`) used to silently collide — the 2nd/3rd calls
    reused the pane slot the 1st call already owned instead of getting their
    own independent pane, orphaning their freshly-created worktrees on disk.
    """

    def test_second_bare_worktree_assign_is_hard_rejected(self, orch, monkeypatch):
        # Simulate call #1 already having registered a pane for "backend".
        orch._panes_by_project["proj"] = {"backend": MagicMock()}
        orch._assign_with_worktree = MagicMock(return_value=(True, "should not be reached"))

        ok, msg = orch.assign(
            "backend", "/repo/api", "task 2", isolation="worktree", project="proj"
        )

        assert ok is False
        assert "#N" in msg or "backend#" in msg
        orch._assign_with_worktree.assert_not_called()

    def test_third_bare_worktree_assign_also_rejected(self, orch):
        orch._panes_by_project["proj"] = {"backend": MagicMock()}
        orch._assign_with_worktree = MagicMock(return_value=(True, "should not be reached"))

        ok1, _ = orch.assign("backend", "/repo/api", "t2", isolation="worktree", project="proj")
        ok2, _ = orch.assign("backend", "/repo/api", "t3", isolation="worktree", project="proj")

        assert ok1 is False
        assert ok2 is False
        orch._assign_with_worktree.assert_not_called()

    def test_shard_suffixed_role_not_blocked(self, orch, monkeypatch):
        # role_name carries its own `#N` instance identity → distinct pane
        # slot → no collision, even though bare "backend" is already alive.
        orch._panes_by_project["proj"] = {"backend": MagicMock()}
        orch._assign_with_worktree = MagicMock(return_value=(True, "ok"))

        ok, _msg = orch.assign("backend#2", "/repo/api", "t2", isolation="worktree", project="proj")

        assert ok is True
        orch._assign_with_worktree.assert_called_once()

    def test_no_existing_pane_is_not_a_collision(self, orch):
        # First-ever worktree assign for this role: no pane registered yet.
        orch._assign_with_worktree = MagicMock(return_value=(True, "ok"))

        ok, _ = orch.assign("backend", "/repo/api", "t1", isolation="worktree", project="proj")

        assert ok is True
        orch._assign_with_worktree.assert_called_once()

    def test_shared_isolation_reassign_to_running_pane_unaffected(self, orch):
        # The guard is scoped to isolation="worktree" only — a normal
        # follow-up task to an already-running pane (shared isolation) must
        # keep working exactly as before.
        orch._panes_by_project["proj"] = {"backend": MagicMock()}
        orch._assign_dispatch = MagicMock(return_value=(True, "ok"))

        ok, _ = orch.assign("backend", "/repo/api", "follow-up", project="proj")

        assert ok is True
        orch._assign_dispatch.assert_called_once()


# ── done/close finalize ─────────────────────────────────────────────────────


class TestFinalizeWorktree:
    def test_commits_produce_merge_proposal(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info(), commits=3, merge_conflicts=False)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "frontend", _info().as_dict())
        assert fake.safe_remove_calls == 0  # never removed — Lead merges first
        msg = orch._notify_lead.call_args[0][1]
        assert "merge --no-ff wt/frontend-1" in msg
        assert "พร้อม merge" in msg  # clean + merge-tree-clean → readiness claim allowed
        # #464 — commit/file counts already sit in the digest bullet `done()`
        # sends for the same event; the proposal must not repeat them.
        assert "digest" in msg.lower()

    def test_dirty_worktree_with_commits_never_claims_ready_to_merge(self, orch, monkeypatch):
        """#244 near-miss: a branch can carry accepted commits AND still hold
        fresh uncommitted work on top — the proposal must warn instead of
        claiming "พร้อม merge", and must not surface the merge command as an
        available step."""
        fake = _FakeMgr(info=_info(), commits=2, dirty=True, uncommitted=5)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "backend", _info().as_dict())
        msg = orch._notify_lead.call_args[0][1]
        assert "พร้อม merge" not in msg
        assert "5 ไฟล์ที่ยังไม่ commit" in msg
        assert "merge --no-ff" not in msg  # not offered as a step while dirty

    def test_merge_conflict_against_current_base_warns(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info(), commits=1, dirty=False, merge_conflicts=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "backend", _info().as_dict())
        msg = orch._notify_lead.call_args[0][1]
        assert "conflict" in msg.lower()
        assert "พร้อม merge" not in msg

    def test_unknown_merge_status_does_not_claim_ready(self, orch, monkeypatch):
        fake = _FakeMgr(info=_info(), commits=1, dirty=False, merge_conflicts=None)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "backend", _info().as_dict())
        msg = orch._notify_lead.call_args[0][1]
        assert "พร้อม merge" not in msg
        assert "unknown" in msg.lower()

    def test_empty_clean_worktree_is_kept_and_warns(self, orch, monkeypatch):
        # #161: a zero-commit worktree must NEVER be auto-removed, even when
        # clean — only an explicit `takkub worktree clean` may delete it.
        fake = _FakeMgr(info=_info(), commits=0, dirty=False)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict())
        assert fake.safe_remove_calls == 0  # never removed automatically
        assert fake.auto_commit_calls == 0  # nothing dirty — never attempted
        assert orch._notify_lead.called
        warn = orch._notify_lead.call_args[0][1]
        assert "เก็บไว้ไม่ลบอัตโนมัติ" in warn
        assert "ไม่มี commit" in warn
        assert "worktree clean" in warn

    def test_dirty_worktree_with_no_commits_gets_auto_committed_and_proposed(
        self, orch, monkeypatch
    ):
        """#525: a pane that reports done with real uncommitted work but zero
        commits used to get only the "no commit kept" warning below — no
        merge proposal ever went out, and Lead had to commit by hand before
        anything could merge. The zero-commits branch must now try an
        auto-commit snapshot first and, on success, propose a merge exactly
        like a pane that remembered to commit itself."""
        fake = _FakeMgr(info=_info(), commits=0, dirty=True, uncommitted=3, auto_commit_result=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict())
        assert fake.auto_commit_calls == 1
        assert fake.safe_remove_calls == 0  # still never auto-removed
        msg = orch._notify_lead.call_args[0][1]
        assert "merge --no-ff" in msg  # a real proposal, not the warn-only path
        assert "เก็บไว้ไม่ลบอัตโนมัติ" not in msg

    def test_dirty_worktree_kept_and_warns_when_auto_commit_fails(self, orch, monkeypatch):
        """The fallback path from before #525 must still hold when the
        auto-commit attempt itself fails (e.g. a rejected pre-commit hook) —
        this must never become a NEW way to lose state."""
        fake = _FakeMgr(info=_info(), commits=0, dirty=True, auto_commit_result=False)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict())
        assert fake.auto_commit_calls == 1
        assert fake.safe_remove_calls == 0  # never removed automatically
        warn = orch._notify_lead.call_args[0][1]
        assert "เก็บไว้ไม่ลบอัตโนมัติ" in warn  # kept, not lost
        assert "uncommitted changes" in warn

    def test_no_commit_but_branch_already_merged_suppresses_false_alarm(self, orch, monkeypatch):
        """#536: `commit_count` can legitimately read 0 even though real work
        happened and Lead already merged it (`rediscover_worktree`'s
        fallback `base_sha` collapses to the branch tip once it's merged).
        HEAD having moved past the recorded `base_sha`, plus an ancestry
        check confirming the branch IS merged, must produce a benign notice
        instead of the "งานหายไปจริงหรือแค่ลืม commit" false alarm."""
        fake = _FakeMgr(
            info=_info(),
            commits=0,
            dirty=False,
            head_sha="mergedsha123",
            already_merged=True,
        )
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "backend", _info().as_dict())
        assert fake.safe_remove_calls == 0
        msg = orch._notify_lead.call_args[0][1]
        assert "merge เข้า base ไปแล้ว" in msg
        assert "งานหายไปจริง" not in msg
        assert "เก็บไว้ไม่ลบอัตโนมัติ" not in msg

    def test_virgin_worktree_still_alarms_even_when_trivially_an_ancestor(self, orch, monkeypatch):
        """#161 regression guard: a worktree that never diverged from its
        base (HEAD == base_sha) is ALWAYS a trivial ancestor of the current
        base — the #536 fix must never use ancestry alone to suppress the
        alarm; it must require HEAD to have actually moved first."""
        fake = _FakeMgr(info=_info(), commits=0, dirty=False, already_merged=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "backend", _info().as_dict())
        warn = orch._notify_lead.call_args[0][1]
        assert "ไม่มี commit" in warn
        assert "เก็บไว้ไม่ลบอัตโนมัติ" in warn

    def test_done_note_becomes_auto_commit_snapshot_summary(self, orch, monkeypatch):
        """#536: the pane's own done() note should reach `auto_commit_
        snapshot` as the commit headline instead of the generic message."""
        fake = _FakeMgr(info=_info(), commits=0, dirty=True, auto_commit_result=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        orch._finalize_worktree("proj", "qa", _info().as_dict(), note="implemented the retry queue")
        assert fake.last_auto_commit_summary == "implemented the retry queue"

    def test_finalize_never_raises(self, orch, monkeypatch):
        # A malformed worktree dict must not break done()/close().
        orch._finalize_worktree("proj", "qa", {"bogus": True})
        # no exception; nothing proposed
        assert not orch._notify_lead.called


class TestLiveWorktreePaths:
    """Orchestrator.live_worktree_paths — the pane-registry side of the #187
    live-pane guard `takkub worktree clean` uses before touching any
    checkout. Provider-agnostic on purpose: it keys off `pane.session` +
    `PaneState.worktree`, never anything claude-specific (#103)."""

    @staticmethod
    def _pane(alive: bool) -> MagicMock:
        pane = MagicMock()
        pane.session = MagicMock(is_alive=True) if alive else None
        return pane

    def test_alive_pane_with_worktree_is_reported(self, orch):
        orch._panes_by_project["proj"] = {"frontend": self._pane(alive=True)}
        orch._pane_state["proj::frontend"] = PaneState(worktree=_info().as_dict())

        assert orch.live_worktree_paths("proj") == {str(Path("/wt/frontend-1").resolve())}

    def test_dead_pane_is_excluded(self, orch):
        # session exited but PaneState/worktree record hasn't been popped yet
        # (close()/done() do that atomically, but a crash can leave it behind)
        orch._panes_by_project["proj"] = {"frontend": self._pane(alive=False)}
        orch._pane_state["proj::frontend"] = PaneState(worktree=_info().as_dict())

        assert orch.live_worktree_paths("proj") == set()

    def test_alive_shared_cwd_pane_is_excluded(self, orch):
        # normal (non-isolated) pane: alive, but PaneState.worktree is None
        orch._panes_by_project["proj"] = {"backend": self._pane(alive=True)}
        orch._pane_state["proj::backend"] = PaneState()

        assert orch.live_worktree_paths("proj") == set()

    def test_scoped_to_project(self, orch):
        orch._panes_by_project["proj-a"] = {"frontend": self._pane(alive=True)}
        orch._pane_state["proj-a::frontend"] = PaneState(worktree=_info().as_dict())
        orch._panes_by_project["proj-b"] = {}

        assert orch.live_worktree_paths("proj-b") == set()
        assert orch.live_worktree_paths("proj-a") == {str(Path("/wt/frontend-1").resolve())}


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

    def test_writes_restart_reason_marker_for_restore_notice(self, orch, qapp, tmp_path):
        """Issue #232: the CLI path writes the same marker
        `_restart_cockpit`'s auto-triggered reasons do, so the successor's
        `restore_teammates()` can tell a `takkub restart` apart from an
        auto-applied update."""
        marker = tmp_path / "restart-reason.json"
        # The marker write happens synchronously inside request_restart(),
        # before the deferred 200ms QTimer — but that timer must still be
        # drained within THIS test (same pattern as
        # test_replies_ok_and_emits_deferred below) so it never fires later
        # against a since-torn-down `orch`, which crashes the interpreter
        # (proven: leaving it dangling here caused an access violation in
        # the very next test that shares this module-scoped `qapp`).
        fired: list[bool] = []
        orch.restartRequested.connect(lambda: fired.append(True))
        with patch.object(orch_mod, "_RESTART_REASON_FILE", marker):
            ok, _msg = orch.request_restart()
        assert ok
        assert marker.is_file()
        assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "cli"

        import time as _time

        deadline = _time.monotonic() + 2.0
        while not fired and _time.monotonic() < deadline:
            qapp.processEvents()
        assert fired == [True]

    def test_replies_ok_and_emits_deferred(self, orch, qapp, monkeypatch):
        # Real QTimer.singleShot(200, ...) makes this deferred-but-eventually-
        # fires assertion depend on 200ms of real wall-clock time under a
        # CPU-starved xdist worker — flaky (and slow) the same way
        # test_orchestrator_v2_context_hook's timing assert was (see
        # docs/audit test-diet notes). Keep the timer genuinely async (0ms
        # still routes through the Qt event loop, so `fired == []` right
        # after the call is still a real assertion, not a tautology of the
        # patch) but drop the real-time deadline for a bounded event-pump
        # loop instead.
        real_single_shot = QTimer.singleShot
        monkeypatch.setattr(QTimer, "singleShot", lambda ms, cb: real_single_shot(0, cb))

        fired: list[bool] = []
        orch.restartRequested.connect(lambda: fired.append(True))

        ok, msg = orch.request_restart()
        assert ok
        assert "restart" in msg.lower()
        # Deferred: NOT emitted synchronously — the IPC reply must flush first.
        assert fired == []
        # Fires on the event loop on the next tick(s) — bounded by iteration
        # count, not wall-clock time.
        for _ in range(200):
            qapp.processEvents()
            if fired:
                break
        assert fired == [True]
