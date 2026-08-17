"""Tests for screenshot evidence auto-attach on done() (issue #5).

- assign_ts is captured on PaneState at assign time, read back BEFORE done()
  pops state.
- done() scans the pane's artifacts dir (`runtime/exports/<date>/<project>/`,
  including `screenshots/`) for images newer than assign_ts and at least
  _EVIDENCE_SETTLE_SEC old, and appends a `📸 evidence: …` line to the note.
- qa/critic/designer/reviewer with zero new shots get a `⚠ no evidence cited`
  warning instead, UNLESS the note itself cites a path-like or test-result
  token (see `_EVIDENCE_CITE_RE`) — every other role stays silent.
- `done --fail` gets the same evidence treatment as a clean done.
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState

# ─────────────────────────────────────────────────────────────
# Fixtures (mirrors tests/test_cross_tab_done.py)
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.state = "working"
    p.set_state = MagicMock()
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    """Minimal Orchestrator with I/O mocked out."""
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    return o


def _register_pane(orch: Orchestrator, role: str, project: str, session=None) -> MagicMock:
    pane = _make_pane(session)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


def _mock_done(orch: Orchestrator) -> None:
    orch._save_decision_note = MagicMock()  # type: ignore[assignment]
    orch._write_hot_md = MagicMock()  # type: ignore[assignment]


def _shot_dir(tmp_path, project: str, sub: str = "screenshots"):
    today = time.strftime("%Y-%m-%d")
    d = tmp_path / "exports" / today / project / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch_old_enough(path, assign_ts: float, age: float = 2.0) -> None:
    """Write a file whose mtime sits `age` seconds after assign_ts — old
    enough to be considered settled (past _EVIDENCE_SETTLE_SEC). Content is
    keyed on the filename so unrelated fixtures calling this per-file don't
    accidentally produce byte-identical files and trip #182's dup-of tag."""
    path.write_bytes(b"fake-image-bytes:" + path.name.encode())
    import os

    mt = assign_ts + age
    os.utime(path, (mt, mt))


# ─────────────────────────────────────────────────────────────
# assign_ts capture-before-pop
# ─────────────────────────────────────────────────────────────


class TestAssignTsCapture:
    def test_assign_ts_set_by_assign_dispatch(self, orch, monkeypatch):
        """_assign_dispatch stamps PaneState.assign_ts on a successful spawn."""
        monkeypatch.setattr(orch, "spawn", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(orch, "_send_when_ready", lambda *a, **kw: None)
        monkeypatch.setattr(orch, "_apply_session_goal", lambda task, ns: task)

        before = time.time()
        orch._assign_dispatch("backend", "/repo", "do the thing", project="proj")
        after = time.time()

        ps = orch._pane_state["proj::backend"]
        assert before <= ps.assign_ts <= after

    def test_done_reads_assign_ts_before_pop(self, orch, monkeypatch):
        """done() must read assign_ts from state before the pop (else it's lost)."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        stamp = time.time() - 100
        orch._pane_state["proj::backend"] = PaneState(assign_ts=stamp)

        captured = {}
        orig = Orchestrator._scan_done_evidence.__func__

        def spy(cls, project_ns, from_role, assign_ts, note=""):
            captured["assign_ts"] = assign_ts
            return orig(cls, project_ns, from_role, assign_ts, note)

        monkeypatch.setattr(Orchestrator, "_scan_done_evidence", classmethod(spy))

        orch.done("backend", note="done", project=proj)

        assert captured["assign_ts"] == stamp
        # state popped after done()
        assert "proj::backend" not in orch._pane_state


# ─────────────────────────────────────────────────────────────
# assign_base_sha capture (#245 — shared-tree digest-facts baseline)
# ─────────────────────────────────────────────────────────────


class TestAssignBaseShaCapture:
    def test_shared_cwd_dispatch_snapshots_head_and_dirty_paths(self, orch, monkeypatch):
        """_assign_dispatch (worktree=None, the shared-cwd path) snapshots
        HEAD plus cheap dirty-path metadata right after spawn resolves cwd."""
        from agent_takkub import worktree_manager as wm_mod

        monkeypatch.setattr(orch, "spawn", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(orch, "_send_when_ready", lambda *a, **kw: None)
        monkeypatch.setattr(orch, "_apply_session_goal", lambda task, ns: task)
        pane = _register_pane(orch, "backend", "proj")
        pane._session_cwd = "/repo/api"

        class _FakeMgr:
            def shared_tree_baseline(self, cwd):
                assert cwd == "/repo/api"
                return "deadbeef", "/repo", {"stale.png": ("??", 123, 456)}

        monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: _FakeMgr())

        orch._assign_dispatch("backend", "/repo/api", "do the thing", project="proj")

        ps = orch._pane_state["proj::backend"]
        assert ps.assign_base_sha == "deadbeef"
        assert ps.assign_git_root == "/repo"
        assert ps.assign_dirty_snapshot == {"stale.png": ("??", 123, 456)}

    def test_no_resolved_cwd_leaves_baseline_none(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "spawn", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(orch, "_send_when_ready", lambda *a, **kw: None)
        monkeypatch.setattr(orch, "_apply_session_goal", lambda task, ns: task)
        # No pane registered → _session_cwd unresolvable → no git call, None.

        orch._assign_dispatch("backend", "/repo", "do the thing", project="proj")

        ps = orch._pane_state["proj::backend"]
        assert ps.assign_base_sha is None
        assert ps.assign_git_root is None
        assert ps.assign_dirty_snapshot is None

    def test_worktree_dispatch_leaves_baseline_none(self, orch, monkeypatch):
        """An isolated worktree pane already has the equivalent baseline in
        WorktreeInfo.base_sha — assign_base_sha must stay unset there."""
        monkeypatch.setattr(orch, "spawn", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(orch, "_send_when_ready", lambda *a, **kw: None)
        monkeypatch.setattr(orch, "_apply_session_goal", lambda task, ns: task)
        _register_pane(orch, "backend", "proj")

        orch._assign_dispatch(
            "backend",
            "/wt/backend-1",
            "do the thing",
            project="proj",
            worktree={
                "path": "/wt/backend-1",
                "branch": "wt/backend-1",
                "base_sha": "b",
                "git_root": "/repo",
            },
        )

        ps = orch._pane_state["proj::backend"]
        assert ps.assign_base_sha is None
        assert ps.assign_git_root is None
        assert ps.assign_dirty_snapshot is None


# ─────────────────────────────────────────────────────────────
# mtime + settle filter
# ─────────────────────────────────────────────────────────────


class TestEvidenceScanFiltering:
    def test_new_image_after_assign_is_evidence(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        _touch_old_enough(shots / "after.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "📸 evidence:" in result
        assert "after.png" in result
        assert "/" in result  # forward slashes

    def test_image_before_assign_is_ignored(self, orch, tmp_path):
        assign_ts = time.time() - 10
        shots = _shot_dir(tmp_path, "proj")
        _touch_old_enough(shots / "stale.png", assign_ts, age=-1000)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "stale.png" not in result
        assert result == "⚠ no evidence cited"

    def test_image_too_fresh_is_settling_and_ignored(self, orch, tmp_path):
        """A file modified within the last _EVIDENCE_SETTLE_SEC is treated as
        still being written and excluded (half-written PNG guard)."""
        assign_ts = time.time() - 5
        shots = _shot_dir(tmp_path, "proj")
        path = shots / "midwrite.png"
        path.write_bytes(b"fake")
        import os

        now = time.time()
        os.utime(path, (now, now))  # freshly touched, inside settle window

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "midwrite.png" not in result

    def test_non_image_files_ignored(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        _touch_old_enough(shots / "notes.txt", assign_ts, age=10)
        _touch_old_enough(shots / "trace.log", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert result == "⚠ no evidence cited"

    def test_evidence_scanned_recursively_under_artifacts_dir(self, orch, tmp_path):
        """Not just screenshots/ — the whole per-project artifacts dir counts."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        root = tmp_path / "exports" / today / "proj"
        root.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(root / "top-level.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "top-level.png" in result

    def test_max_files_cap(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        for i in range(15):
            _touch_old_enough(shots / f"shot{i}.png", assign_ts, age=10 + i)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert result.count(".png") == 10

    def test_no_assign_ts_yields_nothing(self, orch, tmp_path):
        """assign_ts <= 0 (never assigned via _assign_dispatch) → no scan, no warning."""
        shots = _shot_dir(tmp_path, "proj")
        # even with images present, unknown window means we say nothing
        (shots / "whatever.png").write_bytes(b"x")

        result = Orchestrator._scan_done_evidence("proj", "qa", 0.0)

        assert result == ""

    def test_missing_artifacts_dir_degrades_silently(self, orch, tmp_path):
        result = Orchestrator._scan_done_evidence("nonexistent-project", "qa", time.time() - 60)
        assert result == "⚠ no evidence cited"


# ─────────────────────────────────────────────────────────────
# Per-role subdir attribution (issue #109)
# ─────────────────────────────────────────────────────────────


class TestPerRoleSubdirAttribution:
    def test_role_subdir_evidence_preferred_over_shared(self, orch, tmp_path):
        """A pane's own <artifacts_dir>/<role>/ image wins over the flat
        project dir and is NOT tagged (shared dir) — confidently attributed."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        role_dir = tmp_path / "exports" / today / "proj" / "backend"
        role_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(role_dir / "mine.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "backend", assign_ts)

        assert "📸 evidence:" in result
        assert "mine.png" in result
        assert "(shared dir)" not in result

    def test_role_subdir_screenshots_nested_also_found(self, orch, tmp_path):
        assign_ts = time.time() - 60
        role_shots = _shot_dir(tmp_path, "proj/qa", sub="screenshots")
        _touch_old_enough(role_shots / "own.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "own.png" in result
        assert "(shared dir)" not in result

    def test_falls_back_to_shared_dir_when_no_role_subdir(self, orch, tmp_path):
        """No per-role subdir at all (e.g. qa's shots saved flat, the
        pre-existing convention critic pickup relies on) — old flat scan
        still runs, tagged (shared dir) since it's not pane-attributable.
        Scoped to a warn-role (qa) — see #165 below for the non-warn-role
        case, which must NOT fall back."""
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        _touch_old_enough(shots / "qa-shot.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "📸 evidence:" in result
        assert "qa-shot.png" in result
        assert "(shared dir)" in result

    def test_role_subdir_scan_scoped_to_own_role_only(self, orch, tmp_path):
        """The #109 repro fixed: with both panes using their own subdir,
        backend's done() sees only backend's file, never qa's sibling one —
        role_dir scanning is scoped to that one subdir, not the whole tree."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        qa_dir = tmp_path / "exports" / today / "proj" / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(qa_dir / "qa-only.png", assign_ts, age=10)
        backend_dir = tmp_path / "exports" / today / "proj" / "backend"
        backend_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(backend_dir / "backend-only.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "backend", assign_ts)

        assert "backend-only.png" in result
        assert "qa-only.png" not in result
        assert "(shared dir)" not in result

    def test_fallback_can_still_cross_attribute_when_own_subdir_empty(self, orch, tmp_path):
        """Documents the residual, tagged case: if critic has no subdir
        evidence of its own, the old flat/recursive fallback still runs
        (backward compat, warn-roles only — see #165) and can surface qa's
        file — but callers can tell it's not pane-exclusive from the
        (shared dir) tag."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        qa_dir = tmp_path / "exports" / today / "proj" / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(qa_dir / "qa-only.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "critic", assign_ts)

        assert "qa-only.png" in result
        assert "(shared dir)" in result

    def test_non_warn_role_never_cross_attributes_shared_dir(self, orch, tmp_path):
        """Issue #165: a role outside _EVIDENCE_WARN_ROLES (backend, a
        pure-Python pane) must NOT inherit another role's (critic's)
        screenshots via the flat fallback, even when both assign windows
        overlap and backend's own subdir is empty. Confirmed live: a
        backend done() report was tagged with critic's unrelated screenshots
        this way, misleading Lead into trusting evidence backend never
        produced."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        critic_dir = tmp_path / "exports" / today / "proj" / "critic"
        critic_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(critic_dir / "critic-only.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "backend", assign_ts)

        assert result == ""
        assert "critic-only.png" not in result

    def test_non_warn_role_still_gets_its_own_subdir_evidence(self, orch, tmp_path):
        """The #165 fix only removes the cross-role fallback — a non-warn
        role's OWN subdir evidence (self-attributed, no ambiguity) still
        surfaces normally."""
        assign_ts = time.time() - 60
        today = time.strftime("%Y-%m-%d")
        backend_dir = tmp_path / "exports" / today / "proj" / "backend"
        backend_dir.mkdir(parents=True, exist_ok=True)
        _touch_old_enough(backend_dir / "mine.png", assign_ts, age=10)

        result = Orchestrator._scan_done_evidence("proj", "backend", assign_ts)

        assert "📸 evidence:" in result
        assert "mine.png" in result
        assert "(shared dir)" not in result

    def test_shared_tag_does_not_break_max_files_cap(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        for i in range(15):
            _touch_old_enough(shots / f"shot{i}.png", assign_ts, age=10 + i)

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert result.count(".png") == 10
        assert result.endswith("(shared dir)")


# ─────────────────────────────────────────────────────────────
# PermissionError retry
# ─────────────────────────────────────────────────────────────


class TestPermissionErrorRetry:
    def test_stat_retries_then_succeeds(self, orch, tmp_path, monkeypatch):
        path = tmp_path / "locked.png"
        path.write_bytes(b"x")
        real_stat = pathlib.Path.stat
        calls = {"n": 0}

        def flaky_stat(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("locked")
            return real_stat(self)

        monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)

        result = Orchestrator._evidence_stat_mtime(path)

        assert result is not None
        assert calls["n"] == 3

    def test_stat_gives_up_after_max_retries(self, orch, tmp_path, monkeypatch):
        path = tmp_path / "always-locked.png"
        path.write_bytes(b"x")

        def always_raise(self):
            raise PermissionError("locked")

        monkeypatch.setattr(pathlib.Path, "stat", always_raise)

        result = Orchestrator._evidence_stat_mtime(path)

        assert result is None

    def test_locked_file_does_not_break_scan(self, orch, tmp_path, monkeypatch):
        """A file that never unlocks is skipped, not a done()-crashing exception."""
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        good = shots / "good.png"
        _touch_old_enough(good, assign_ts, age=10)
        locked = shots / "locked.png"
        _touch_old_enough(locked, assign_ts, age=10)

        def flaky(path):
            if path.name == "locked.png":
                return None
            return path.stat().st_mtime

        monkeypatch.setattr(Orchestrator, "_evidence_stat_mtime", staticmethod(flaky))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "good.png" in result
        assert "locked.png" not in result


# ─────────────────────────────────────────────────────────────
# append format + warning scoped to qa/critic/designer only
# ─────────────────────────────────────────────────────────────


class TestDoneNoticeAppendFormat:
    def test_evidence_appended_to_done_notice(self, orch, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)
        shots = _shot_dir(tmp_path, proj)
        _touch_old_enough(shots / "login.png", assign_ts, age=10)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        orch.done("qa", note="all green", project=proj)

        assert captured
        assert captured[0].startswith("[qa done] all green")
        assert "📸 evidence:" in captured[0]
        assert "login.png" in captured[0]

    def test_warning_only_for_qa_critic_designer_reviewer(self, orch, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())

        assign_ts = time.time() - 60
        for role in ("qa", "critic", "designer", "reviewer", "backend", "devops"):
            _register_pane(orch, role, proj, _make_alive_session())
            orch._pane_state[f"{proj}::{role}"] = PaneState(assign_ts=assign_ts)

        captured: dict[str, str] = {}
        monkeypatch.setattr(
            orch,
            "_notify_lead",
            lambda ns, notice, from_role=None, **kw: captured.__setitem__(from_role, notice),
        )

        for role in ("qa", "critic", "designer", "reviewer", "backend", "devops"):
            orch.done(role, note="finished", project=proj)

        for warn_role in ("qa", "critic", "designer", "reviewer"):
            assert "⚠ no evidence cited" in captured[warn_role], warn_role

        for quiet_role in ("backend", "devops"):
            assert "⚠ no evidence cited" not in captured[quiet_role], quiet_role
            assert captured[quiet_role] == f"[{quiet_role} done] finished"

    def test_note_citing_path_suppresses_warning(self, orch, tmp_path, monkeypatch):
        """A warn-role note that cites a path-like/test-result token is
        trusted at face value even when the screenshot scan finds nothing."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "reviewer", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::reviewer"] = PaneState(assign_ts=assign_ts)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        orch.done("reviewer", note="reviewed, see docs/review-notes.md", project=proj)

        assert captured
        assert "⚠ no evidence cited" not in captured[0]
        assert captured[0] == "[reviewer done] reviewed, see docs/review-notes.md"

    def test_note_without_citation_gets_tagged(self, orch, tmp_path, monkeypatch):
        """A warn-role note with no path/test-result reference and no scanned
        files gets tagged, so Lead can see the claim is unsubstantiated."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        orch.done("qa", note="all good, ship it", project=proj)

        assert captured
        assert "⚠ no evidence cited" in captured[0]

    def test_note_without_citation_but_scan_finds_files_still_gets_shots(
        self, orch, tmp_path, monkeypatch
    ):
        """Filesystem evidence wins outright — a bare note doesn't suppress
        the 📸 evidence line when the scan actually found something."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)
        shots = _shot_dir(tmp_path, proj)
        _touch_old_enough(shots / "smoke.png", assign_ts, age=10)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        orch.done("qa", note="all good, ship it", project=proj)

        assert captured
        assert "📸 evidence:" in captured[0]
        assert "smoke.png" in captured[0]
        assert "⚠ no evidence cited" not in captured[0]

    def test_done_fail_also_gets_evidence(self, orch, tmp_path, monkeypatch):
        """`done --fail` attaches evidence the same way a clean done does."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)
        shots = _shot_dir(tmp_path, proj)
        _touch_old_enough(shots / "fail-shot.png", assign_ts, age=10)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        orch.done("qa", note="login smoke failed: 500", project=proj, failed=True)

        assert captured
        assert "FAILED" in captured[0]
        assert "📸 evidence:" in captured[0]
        assert "fail-shot.png" in captured[0]

    def test_shard_pane_evidence_folds_into_note_for_aggregate(self, orch, tmp_path, monkeypatch):
        """Shard panes suppress their own Lead notice, but the evidence-bearing
        `note` still lands in the shard group's aggregate (group.done)."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa#1", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa#1"] = PaneState(assign_ts=assign_ts, shard_total=2)
        shots = _shot_dir(tmp_path, proj)
        _touch_old_enough(shots / "shard1.png", assign_ts, age=10)

        from agent_takkub.pipeline_executor import ShardGroup

        group = ShardGroup(base_role="qa", total=2)
        orch._shard_groups = {f"{proj}::qa": group}
        monkeypatch.setattr(orch, "_inject_shard_fanout_handoff", lambda *a, **kw: None)

        orch.done("qa#1", note="shard 1 done", project=proj)

        assert "📸 evidence:" in group.done["qa#1"]
        assert "shard1.png" in group.done["qa#1"]


# ─────────────────────────────────────────────────────────────
# Suspect-capture flagging (issue #159): a screenshot can "exist" and still
# be a failed/blank capture — size + magic-byte sniff surfaces that instead
# of trusting file existence alone as proof of a good shot.
# ─────────────────────────────────────────────────────────────


def _write_real_png(path: pathlib.Path, extra_bytes: int = 0) -> None:
    """A file that starts with a real PNG signature, padded to look like a
    normal-sized screenshot."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * extra_bytes)


class TestSuspectCaptureFlagging:
    def test_size_annotated_on_every_entry(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        path = shots / "big.png"
        _write_real_png(path, extra_bytes=50 * 1024)
        import os

        mt = assign_ts + 10
        os.utime(path, (mt, mt))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "big.png (50.0KB)" in result
        assert "⚠" not in result

    def test_small_file_flagged_suspect(self, orch, tmp_path):
        """A real-looking but tiny file (< 10KB) is still evidence, but
        tagged so Lead can tell it might be a failed capture."""
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        path = shots / "tiny.png"
        _write_real_png(path, extra_bytes=100)  # well under 10KB
        import os

        mt = assign_ts + 10
        os.utime(path, (mt, mt))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "📸 evidence:" in result
        assert "tiny.png" in result
        assert "⚠small" in result
        assert "⚠ no evidence cited" not in result  # still counts as evidence

    def test_bad_header_flagged_suspect_even_if_large(self, orch, tmp_path):
        """Size alone isn't the whole story — wrong content under a .png
        extension gets flagged regardless of byte count."""
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        path = shots / "notreally.png"
        _touch_old_enough(path, assign_ts, age=10)  # b"fake-image-bytes", no PNG magic
        # pad well past the size threshold so only the header check trips
        path.write_bytes(b"fake-image-bytes" + b"\x00" * 20 * 1024)
        import os

        mt = assign_ts + 10
        os.utime(path, (mt, mt))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "⚠bad-header" in result
        assert "⚠small" not in result

    def test_both_small_and_bad_header_combine(self, orch, tmp_path):
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        path = shots / "empty.png"
        _touch_old_enough(path, assign_ts, age=10)  # tiny + no PNG magic
        os_utime_target = assign_ts + 10
        import os

        os.utime(path, (os_utime_target, os_utime_target))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "⚠small+bad-header" in result

    def test_evidence_format_entry_direct(self, tmp_path):
        good = tmp_path / "shot.png"
        _write_real_png(good, extra_bytes=50 * 1024)
        entry = Orchestrator._evidence_format_entry(good, good.stat().st_size)
        assert entry.endswith("(50.0KB)")

        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not-a-png")
        entry_bad = Orchestrator._evidence_format_entry(bad, bad.stat().st_size)
        assert "⚠small+bad-header" in entry_bad

    def test_evidence_looks_valid_image_per_format(self, tmp_path):
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        assert Orchestrator._evidence_looks_valid_image(png) is True

        jpg = tmp_path / "a.jpg"
        jpg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        assert Orchestrator._evidence_looks_valid_image(jpg) is True

        bogus_png = tmp_path / "b.png"
        bogus_png.write_bytes(b"<html>error</html>")
        assert Orchestrator._evidence_looks_valid_image(bogus_png) is False

        empty_png = tmp_path / "c.png"
        empty_png.write_bytes(b"")
        assert Orchestrator._evidence_looks_valid_image(empty_png) is False


class TestDuplicateContentFlagging:
    """Issue #182: a byte-identical screenshot filed under several names
    passes every #159 size/header check but tells Lead nothing distinct —
    a live case slipped through until Lead manually diff'd md5 sums."""

    def test_byte_identical_files_flag_the_later_ones(self, orch, tmp_path) -> None:
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (20 * 1024)
        first = shots / "r3_04_login.png"
        second = shots / "r3_05_dashboard.png"
        third = shots / "r3_06_confirm.png"
        for i, p in enumerate((first, second, third)):
            p.write_bytes(payload)
            import os

            mt = assign_ts + 10 + i  # distinct mtimes so sort order is deterministic
            os.utime(p, (mt, mt))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        # Newest-first ordering (see _scan_done_evidence's sort) — third.mtime
        # is newest, so it's the first-seen occurrence and stays untagged;
        # second and first repeat its hash and get flagged.
        assert f"⚠dup-of:{third.name}" in result
        assert "r3_05_dashboard.png" in result
        assert "r3_04_login.png" in result
        assert result.count("dup-of") == 2

    def test_distinct_content_not_flagged_as_duplicate(self, orch, tmp_path) -> None:
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        a = shots / "a.png"
        b = shots / "b.png"
        _write_real_png(a, extra_bytes=10 * 1024)
        _write_real_png(b, extra_bytes=20 * 1024)  # different size → different bytes
        import os

        os.utime(a, (assign_ts + 10, assign_ts + 10))
        os.utime(b, (assign_ts + 11, assign_ts + 11))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "dup-of" not in result

    def test_duplicate_combines_with_small_tag(self, orch, tmp_path) -> None:
        assign_ts = time.time() - 60
        shots = _shot_dir(tmp_path, "proj")
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # under the 10KB suspect floor
        first = shots / "one.png"
        second = shots / "two.png"
        first.write_bytes(payload)
        second.write_bytes(payload)
        import os

        os.utime(first, (assign_ts + 10, assign_ts + 10))
        os.utime(second, (assign_ts + 11, assign_ts + 11))

        result = Orchestrator._scan_done_evidence("proj", "qa", assign_ts)

        assert "⚠small" in result
        assert "dup-of" in result

    def test_evidence_content_hash_skips_files_over_dedup_cap(self, tmp_path) -> None:
        big = tmp_path / "huge.png"
        big.write_bytes(b"\x89PNG\r\n\x1a\n")
        oversized = orch_mod._EVIDENCE_DEDUP_MAX_BYTES + 1
        assert Orchestrator._evidence_content_hash(big, oversized) is None

    def test_evidence_content_hash_stable_for_identical_bytes(self, tmp_path) -> None:
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        payload = b"\x89PNG\r\n\x1a\n" + b"same"
        a.write_bytes(payload)
        b.write_bytes(payload)
        ha = Orchestrator._evidence_content_hash(a, a.stat().st_size)
        hb = Orchestrator._evidence_content_hash(b, b.stat().st_size)
        assert ha == hb
        assert ha is not None

    def test_evidence_format_entry_tags_dup_of(self, tmp_path) -> None:
        shot = tmp_path / "shot.png"
        original = tmp_path / "original.png"
        _write_real_png(shot, extra_bytes=50 * 1024)
        entry = Orchestrator._evidence_format_entry(shot, shot.stat().st_size, dup_of=original)
        assert "⚠dup-of:original.png" in entry


class TestFailureAutoCapture:
    """`done(failed=True)` auto-captures into role-memory (ReflexionMemory-style,
    no agent decision required) — wired at the same point evidence attach is."""

    def test_done_fail_writes_role_memory_entry(self, orch, tmp_path, monkeypatch):
        from agent_takkub import role_memory as rm_mod

        monkeypatch.setattr(rm_mod, "ROLE_MEMORY_DIR", tmp_path / "role-memory")
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())
        orch._pane_state[f"{proj}::backend"] = PaneState(assign_ts=time.time() - 60)
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: None)

        orch.done("backend", note="migration crashed on empty table", project=proj, failed=True)

        mem_path = rm_mod.role_memory_path(proj, "backend")
        assert mem_path.exists()
        text = mem_path.read_text(encoding="utf-8")
        assert "migration crashed on empty table" in text
        assert "fail —" in text

    def test_done_fail_uses_first_line_only(self, orch, tmp_path, monkeypatch):
        from agent_takkub import role_memory as rm_mod

        monkeypatch.setattr(rm_mod, "ROLE_MEMORY_DIR", tmp_path / "role-memory")
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=time.time() - 60)
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: None)

        orch.done(
            "qa", note="checkout 500 on submit\nfull stack trace here...", project=proj, failed=True
        )

        text = rm_mod.role_memory_path(proj, "qa").read_text(encoding="utf-8")
        assert "checkout 500 on submit" in text
        assert "full stack trace here" not in text

    def test_shard_pane_fail_captures_under_base_role(self, orch, tmp_path, monkeypatch):
        from agent_takkub import role_memory as rm_mod

        monkeypatch.setattr(rm_mod, "ROLE_MEMORY_DIR", tmp_path / "role-memory")
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa#1", proj, _make_alive_session())
        orch._pane_state[f"{proj}::qa#1"] = PaneState(assign_ts=time.time() - 60, shard_total=2)
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: None)

        from agent_takkub.pipeline_executor import ShardGroup

        group = ShardGroup(base_role="qa", total=2)
        orch._shard_groups = {f"{proj}::qa": group}
        monkeypatch.setattr(orch, "_inject_shard_fanout_handoff", lambda *a, **kw: None)

        orch.done(
            "qa#1", note="shard flake: timeout waiting for selector", project=proj, failed=True
        )

        text = rm_mod.role_memory_path(proj, "qa").read_text(encoding="utf-8")
        assert "shard flake: timeout waiting for selector" in text

    def test_done_fail_role_memory_error_does_not_break_done(self, orch, tmp_path, monkeypatch):
        """A role-memory I/O failure during capture must not break the FAILED
        report itself — Lead still gets notified."""
        from agent_takkub import role_memory as rm_mod

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(rm_mod, "append_failure_entry", boom)
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())
        orch._pane_state[f"{proj}::backend"] = PaneState(assign_ts=time.time() - 60)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        ok, _msg = orch.done("backend", note="something broke", project=proj, failed=True)

        assert ok is True
        assert captured and "FAILED" in captured[0]
