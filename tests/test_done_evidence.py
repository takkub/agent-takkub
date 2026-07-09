"""Tests for screenshot evidence auto-attach on done() (issue #5).

- assign_ts is captured on PaneState at assign time, read back BEFORE done()
  pops state.
- done() scans the pane's artifacts dir (`runtime/exports/<date>/<project>/`,
  including `screenshots/`) for images newer than assign_ts and at least
  _EVIDENCE_SETTLE_SEC old, and appends a `📸 evidence: …` line to the note.
- qa/critic/designer with zero new shots get a `⚠ no screenshot evidence`
  warning instead; every other role stays silent.
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
    enough to be considered settled (past _EVIDENCE_SETTLE_SEC)."""
    path.write_bytes(b"fake-image-bytes")
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

        def spy(cls, project_ns, from_role, assign_ts):
            captured["assign_ts"] = assign_ts
            return orig(cls, project_ns, from_role, assign_ts)

        monkeypatch.setattr(Orchestrator, "_scan_done_evidence", classmethod(spy))

        orch.done("backend", note="done", project=proj)

        assert captured["assign_ts"] == stamp
        # state popped after done()
        assert "proj::backend" not in orch._pane_state


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
        assert result == "⚠ no screenshot evidence"

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

        assert result == "⚠ no screenshot evidence"

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
        assert result == "⚠ no screenshot evidence"


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

    def test_warning_only_for_qa_critic_designer(self, orch, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())

        assign_ts = time.time() - 60
        for role in ("qa", "critic", "designer", "backend", "devops", "reviewer"):
            _register_pane(orch, role, proj, _make_alive_session())
            orch._pane_state[f"{proj}::{role}"] = PaneState(assign_ts=assign_ts)

        captured: dict[str, str] = {}
        monkeypatch.setattr(
            orch,
            "_notify_lead",
            lambda ns, notice, from_role=None, **kw: captured.__setitem__(from_role, notice),
        )

        for role in ("qa", "critic", "designer", "backend", "devops", "reviewer"):
            orch.done(role, note="finished", project=proj)

        for warn_role in ("qa", "critic", "designer"):
            assert "⚠ no screenshot evidence" in captured[warn_role], warn_role

        for quiet_role in ("backend", "devops", "reviewer"):
            assert "⚠ no screenshot evidence" not in captured[quiet_role], quiet_role
            assert captured[quiet_role] == f"[{quiet_role} done] finished"

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
