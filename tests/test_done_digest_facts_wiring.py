"""Wiring tests for #245: `done()` computes a `DigestFacts` fact table and
threads it into `_notify_lead(..., digest_facts=...)` — for both an isolated
worktree pane (reusing `_finalize_worktree`'s git reads, not duplicating
them) and a shared-tree pane (using the assign-time HEAD snapshot).

Mirrors the fixture pattern in test_done_note_symmetrize.py: a minimal
Orchestrator with a real `_save_decision_note` (writes into tmp_path) so the
done() path runs end to end; `_notify_lead` is replaced with a capturing
lambda so kwargs (including `digest_facts`) are inspectable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub import worktree_manager as wm_mod
from agent_takkub.digest_facts import DigestFacts
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState
from agent_takkub.worktree_manager import WorktreeInfo


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    return app or QCoreApplication([])


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _make_pane(session=None, cwd: str | None = None) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.state = "working"
    p.set_state = MagicMock()
    p._session_cwd = cwd
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))

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
    monkeypatch.setattr(o, "_write_hot_md", MagicMock())
    return o


def _register_pane(
    orch: Orchestrator, role: str, project: str, session=None, cwd=None
) -> MagicMock:
    pane = _make_pane(session, cwd)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


class _FakeMgr:
    """Fake WorktreeManager used by the worktree-branch tests — same shape
    as test_worktree_assign.py's, extended with the shared-tree generic
    probes so the same fake covers both branches of `_compute_digest_facts`.
    """

    def __init__(
        self,
        commits: int = 2,
        dirty: bool = False,
        uncommitted: int = 0,
        merge_conflicts: bool | None = False,
        diffstat: str = " src/x.ts | 3 +++",
        pushed: bool = False,
    ):
        self.commits = commits
        self.dirty = dirty
        self.uncommitted = uncommitted
        self.merge_conflicts = merge_conflicts
        self.diffstat_text = diffstat
        self.pushed = pushed
        self.commit_count_calls = 0
        self.is_dirty_calls = 0
        self.merge_calls = 0
        self.diffstat_calls = 0
        self.uncommitted_calls = 0
        self.remote_branch_exists_calls = 0

    def commit_count(self, info):
        self.commit_count_calls += 1
        return self.commits

    def is_dirty(self, info):
        self.is_dirty_calls += 1
        return self.dirty

    def uncommitted_count(self, info):
        self.uncommitted_calls += 1
        return self.uncommitted

    def merge_conflicts_with_base(self, git_root, branch):
        self.merge_calls += 1
        return self.merge_conflicts

    def diffstat(self, info):
        self.diffstat_calls += 1
        return self.diffstat_text

    def remote_branch_exists(self, git_root, branch):
        self.remote_branch_exists_calls += 1
        return self.pushed


def _wt_info() -> WorktreeInfo:
    return WorktreeInfo(
        path="/wt/backend-1", branch="wt/backend-1", base_sha="base", git_root="/repo"
    )


class TestWorktreePaneDigestFacts:
    def test_digest_facts_passed_to_notify_lead_and_git_reads_not_duplicated(
        self, orch, monkeypatch
    ):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix issue #245 please",
            worktree=_wt_info().as_dict(),
        )

        fake = _FakeMgr(commits=2, dirty=False, merge_conflicts=False)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("backend", note="แก้เสร็จแล้ว", project=proj)

        # exactly one clean-done notice carried digest_facts
        done_calls = [c for c in captured if c[0].startswith("[backend done]")]
        assert len(done_calls) == 1
        facts = done_calls[0][1]["digest_facts"]
        assert isinstance(facts, DigestFacts)
        assert facts.ref == "#245"
        assert facts.branch == "wt/backend-1"
        assert facts.commits_ahead == 2
        assert facts.uncommitted == 0
        assert facts.merge_conflicts is False
        assert facts.files_touched == 1  # " src/x.ts | 3 +++" → 1 file
        assert facts.pushed is False

        # `_finalize_worktree` (fired later in done()) must reuse the SAME
        # git reads instead of re-running them — each probe ran exactly once
        # even though both digest-fact computation AND the merge-proposal
        # notice need the same numbers.
        assert fake.commit_count_calls == 1
        assert fake.is_dirty_calls == 1
        assert fake.merge_calls == 1
        assert fake.diffstat_calls == 1
        assert fake.remote_branch_exists_calls == 1

    def test_pushed_branch_surfaces_as_pushed_true(self, orch, monkeypatch):
        """#462 — a worktree pane may push its own `wt/*` branch (#438); the
        digest bullet must say so."""
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix issue #245 please",
            worktree=_wt_info().as_dict(),
        )

        fake = _FakeMgr(commits=2, dirty=False, merge_conflicts=False, pushed=True)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("backend", note="แก้เสร็จแล้ว", project=proj)

        done_calls = [c for c in captured if c[0].startswith("[backend done]")]
        facts = done_calls[0][1]["digest_facts"]
        assert facts.pushed is True
        assert facts.branch == "wt/backend-1"

    def test_failed_report_never_carries_digest_facts(self, orch, monkeypatch):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())
        orch._pane_state[f"{proj}::qa"] = PaneState(
            last_assigned_task="verify #245", worktree=_wt_info().as_dict()
        )
        fake = _FakeMgr(commits=1)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("qa", note="checkout broken", project=proj, failed=True)

        failed_calls = [c for c in captured if "FAILED" in c[0]]
        assert len(failed_calls) == 1
        assert failed_calls[0][1].get("digest_facts") is None


class TestWorktreeRediscoveryAfterRestart:
    """#410: a cockpit restart between assign(isolation="worktree") and
    done() can strand PaneState.worktree as None even though the branch
    genuinely carries commits. `git_facts["rediscovered_worktree"]` — what
    `WorktreeManager.collect_done_git_facts`'s own #410 fallback returns —
    must still drive the merge proposal + worktree-shaped digest facts, not
    silently downgrade to the shared-tree "ตรวจไม่ได้" path."""

    def test_rediscovered_worktree_still_produces_merge_proposal(self, orch, monkeypatch):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session(), cwd="/wt/backend-1")
        # This is exactly what a pane restored via restore_teammates() looks
        # like to done() when its snapshot entry carried no worktree/assign_*
        # bookkeeping at all (an older snapshot, or any other loss #410's
        # snapshot_state()/restore_teammates() fix doesn't cover).
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix #245",
            worktree=None,
            assign_base_sha=None,
            assign_git_root=None,
        )

        fake = _FakeMgr(commits=2, dirty=False, merge_conflicts=False)
        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: fake)

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done(
            "backend",
            note="แก้เสร็จแล้ว",
            project=proj,
            git_facts={
                "kind": "worktree",
                "commits": 2,
                "dirty": False,
                "uncommitted": 0,
                "merge_conflicts": False,
                "diffstat": " src/x.ts | 3 +++",
                "rediscovered_worktree": _wt_info().as_dict(),
            },
        )

        _done_notice, done_kw = next(c for c in captured if c[0].startswith("[backend done]"))
        facts = done_kw["digest_facts"]
        assert facts.branch == "wt/backend-1"
        assert facts.commits_ahead == 2
        assert (
            facts.files_note
            != "ตรวจไม่ได้ (snapshot ตอน assign ไม่ครบ — cwd ไม่ใช่ git repo, HEAD ว่าง, หรืออ่าน git status ไม่สำเร็จ)"
        )

        proposal = next(n for n, _kw in captured if "merge --no-ff wt/backend-1" in n)
        assert "2 commit" in proposal

        # Everything came from git_facts / the rediscovered dict — the fake
        # manager must never have been asked to compute anything itself.
        assert fake.commit_count_calls == 0
        assert fake.is_dirty_calls == 0
        assert fake.merge_calls == 0
        assert fake.diffstat_calls == 0

    def test_no_rediscovery_available_still_falls_back_to_shared_tree_message(
        self, orch, monkeypatch
    ):
        # collect_done_git_facts genuinely couldn't reconstruct anything
        # (real shared-tree pane) — must behave exactly as before #410.
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session(), cwd="/repo/api")
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix #245",
            worktree=None,
            assign_base_sha=None,
            assign_git_root=None,
        )

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done(
            "backend",
            note="done",
            project=proj,
            git_facts={"kind": "shared", "branch": "main", "commits_ahead": 0, "porcelain": None},
        )

        facts = next(kw["digest_facts"] for n, kw in captured if n.startswith("[backend done]"))
        assert "ตรวจไม่ได้" in facts.files_note
        assert not any("merge --no-ff" in n for n, _kw in captured)


class TestSharedTreePaneDigestFacts:
    def test_no_snapshot_reports_unverifiable_not_zero(self, orch):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session(), cwd="/repo/api")
        # No assign_base_sha recorded — e.g. cwd wasn't a git repo at assign time.
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix #245", worktree=None, assign_base_sha=None
        )

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("backend", note="done", project=proj)

        facts = next(kw["digest_facts"] for notice, kw in captured if notice.startswith("[backend"))
        assert facts.files_touched is None
        assert "ตรวจไม่ได้" in facts.files_note

    def test_snapshot_present_computes_commits_and_files(self, orch, monkeypatch):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session(), cwd="/repo/api")
        orch._pane_state[f"{proj}::backend"] = PaneState(
            last_assigned_task="fix #245",
            worktree=None,
            assign_base_sha="abc123",
            assign_git_root="/repo",
            assign_dirty_snapshot={
                "stale.png": ("??", None, None),
                "src/other.py": (" M", None, None),
            },
        )

        class _SharedFake:
            def current_branch(self, cwd):
                return "main"

            def uncommitted_count_at(self, cwd):
                return 1

            def diffstat_since(self, cwd, base_sha):
                assert base_sha == "abc123"
                return " src/a.py | 2 +-"

            def commits_since(self, cwd, base_sha):
                return 3

            def shared_tree_status_porcelain(self, cwd):
                return "?? stale.png\n M src/other.py\n?? src/new.py\n"

            def dirty_snapshot(self, git_root, porcelain):
                return wm_mod.snapshot_porcelain_paths(git_root, porcelain)

        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: _SharedFake())

        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("backend", note="done", project=proj)

        facts = next(kw["digest_facts"] for notice, kw in captured if notice.startswith("[backend"))
        assert facts.branch == "main"
        assert facts.commits_ahead == 3
        assert facts.uncommitted == 3  # whole-tree dirt is still labelled honestly
        assert facts.files_touched == 2  # a.py + new.py
        assert facts.files_dirs == ("src",)
        assert facts.merge_conflicts is None
        assert "shared tree" in facts.merge_note
        assert "path/mtime/size" in facts.files_note

    def test_done_status_failure_reports_unverifiable_not_baseline_paths(self, orch, monkeypatch):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session(), cwd="/repo")
        orch._pane_state[f"{proj}::qa"] = PaneState(
            last_assigned_task="verify #251",
            assign_base_sha="abc123",
            assign_git_root="/repo",
            assign_dirty_snapshot={"stale.png": ("??", 100, 10)},
        )

        class _FailedStatusFake:
            def current_branch(self, cwd):
                return "main"

            def commits_since(self, cwd, base_sha):
                return 0

            def shared_tree_status_porcelain(self, cwd):
                return None

        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: _FailedStatusFake())
        captured: list[tuple[str, dict]] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append((notice, kw))  # type: ignore[assignment]

        orch.done("qa", note="done", project=proj)

        facts = next(kw["digest_facts"] for notice, kw in captured if notice.startswith("[qa"))
        assert facts.files_touched is None
        assert "git status ตอน done" in facts.files_note
