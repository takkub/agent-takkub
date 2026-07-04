"""Tests for cross-tab done notification (Fix A) and done-notice queue (Fix B).

Fix A: when done() fires for a non-active project, crossTabDone signal is
       emitted so main_window can flash the status bar for the user.

Fix B: when Lead is absent at done() time, the notice is queued in
       _pending_done_notices and delivered when Lead next spawns.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator

# ─────────────────────────────────────────────────────────────
# Fixtures
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


def _make_dead_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = False
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

    with (
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
    ):
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
    """Patch heavy side-effects in done() that aren't under test."""
    orch._save_decision_note = MagicMock()  # type: ignore[assignment]
    orch._write_hot_md = MagicMock()  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# Fix A — cross-tab signal
# ─────────────────────────────────────────────────────────────


class TestCrossTabDoneSignal:
    def test_signal_emitted_for_background_project(self, orch, monkeypatch):
        """done() in project B while active project is A → crossTabDone emitted."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        emitted: list[tuple] = []
        orch.crossTabDone.connect(lambda p, r, n: emitted.append((p, r, n)))

        proj_b = "proj_b"
        _register_pane(orch, LEAD.name, proj_b, _make_alive_session())
        backend = _register_pane(orch, "backend", proj_b, _make_alive_session())
        backend.state = "working"

        orch.done("backend", note="endpoint ready", project=proj_b)

        assert len(emitted) == 1
        assert emitted[0] == ("proj_b", "backend", "endpoint ready")

    def test_signal_not_emitted_for_active_project(self, orch, monkeypatch):
        """done() in the active project → crossTabDone NOT emitted."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        emitted: list[tuple] = []
        orch.crossTabDone.connect(lambda p, r, n: emitted.append((p, r, n)))

        proj_a = "proj_a"
        _register_pane(orch, LEAD.name, proj_a, _make_alive_session())
        backend = _register_pane(orch, "backend", proj_a, _make_alive_session())
        backend.state = "working"

        orch.done("backend", note="done", project=proj_a)

        assert emitted == []

    def test_signal_carries_correct_fields(self, orch, monkeypatch):
        """crossTabDone args are (project_ns, role, note) in correct order."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("active_proj", {}))
        _mock_done(orch)

        received: list = []
        orch.crossTabDone.connect(lambda p, r, n: received.extend([p, r, n]))

        proj = "bg_proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        orch.done("qa", note="smoke tests green", project=proj)

        assert received == ["bg_proj", "qa", "smoke tests green"]

    def test_signal_not_emitted_when_active_project_unknown(self, orch, monkeypatch):
        """If active_project() raises, no signal and no crash."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: (None, {}))
        _mock_done(orch)

        emitted: list = []
        orch.crossTabDone.connect(lambda p, r, n: emitted.append(p))

        proj = "some_proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())

        orch.done("frontend", note="done", project=proj)

        assert emitted == []


# ─────────────────────────────────────────────────────────────
# Feedback routing — `done --fail` surfaces a fix-loop proposal
# ─────────────────────────────────────────────────────────────


class TestVerifyFailFeedbackRouting:
    def test_done_fail_surfaces_fix_loop_proposal(self, orch, monkeypatch):
        """`done --fail` swaps the plain done note for a fix-loop PROPOSAL to
        Lead (feedback routing MVP) — human-in-the-loop, never auto-fired."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("vp", {}))
        _mock_done(orch)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        proj = "vp"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        orch.done("qa", note="login smoke failed: 500 on submit", project=proj, failed=True)

        assert captured, "Lead must get a notice"
        notice = captured[0]
        assert "FAILED" in notice  # framed as a failure, not a clean done
        assert "login smoke failed" in notice  # failure detail preserved
        assert "fix loop" in notice  # proposes the remediation loop
        assert "[qa done]" not in notice

    def test_done_success_keeps_plain_note(self, orch, monkeypatch):
        """A normal done (failed=False) still yields the plain `[role done]` note."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("vp", {}))
        _mock_done(orch)

        captured: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice))

        proj = "vp"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        orch.done("qa", note="all green", project=proj, failed=False)

        assert captured and captured[0] == "[qa done] all green"


# ─────────────────────────────────────────────────────────────
# Fix B — done-notice queue
# ─────────────────────────────────────────────────────────────


class TestDoneNoticeQueue:
    def test_notice_queued_when_lead_absent(self, orch, monkeypatch):
        """done() with no Lead pane → notice stored in _pending_done_notices."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        proj = "proj_b"
        # Lead pane NOT registered
        backend = _register_pane(orch, "backend", proj, _make_alive_session())
        backend.state = "working"

        orch.done("backend", note="api ready", project=proj)

        pending = orch._pending_done_notices.get(proj, [])
        assert len(pending) == 1
        assert pending[0]["role"] == "backend"
        assert pending[0]["note"] == "api ready"
        assert "[backend done]" in pending[0]["body"]

    def test_notice_queued_when_lead_dead(self, orch, monkeypatch):
        """done() when Lead session is not alive → notice queued, not dropped."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())

        orch.done("frontend", note="UI done", project=proj)

        pending = orch._pending_done_notices.get(proj, [])
        assert len(pending) == 1
        assert pending[0]["role"] == "frontend"

    def test_multiple_notices_queued(self, orch, monkeypatch):
        """Two done() calls with Lead absent → both notices queued in order."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "backend", proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        orch.done("backend", note="endpoints ready", project=proj)
        orch.done("qa", note="tests pass", project=proj)

        pending = orch._pending_done_notices.get(proj, [])
        assert len(pending) == 2
        roles = [item["role"] for item in pending]
        assert roles == ["backend", "qa"]

    def test_done_notice_queued_event_logged(self, orch, tmp_path, monkeypatch):
        """done_notice_queued event is written to events.log when Lead is absent."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)
        _mock_done(orch)

        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "devops", proj, _make_alive_session())

        orch.done("devops", note="deployed", project=proj)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        queued = [e for e in events if e["event"] == "done_notice_queued"]
        assert len(queued) == 1
        assert queued[0]["project"] == proj
        assert queued[0]["role"] == "devops"

    def test_notice_delivered_immediately_when_lead_alive(self, orch, monkeypatch):
        """done() with alive Lead → notice written directly, nothing queued."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))
        _mock_done(orch)

        proj = "proj_b"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.done("backend", note="done", project=proj)

        assert lead_session.write.called
        assert not orch._pending_done_notices.get(proj)


# ─────────────────────────────────────────────────────────────
# Fix B — _flush_pending_done_notices
# ─────────────────────────────────────────────────────────────


class TestFlushPendingDoneNotices:
    def test_flush_delivers_queued_notices(self, orch, monkeypatch):
        """_flush_pending_done_notices writes all queued bodies to Lead."""
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_a", {}))

        proj = "proj_b"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)

        orch._pending_done_notices[proj] = [
            {"role": "backend", "note": "api done", "body": "[backend done] api done"},
            {"role": "qa", "note": "tests pass", "body": "[qa done] tests pass"},
        ]

        orch._flush_pending_done_notices(proj)

        assert lead_session.write.called
        assert proj not in orch._pending_done_notices

    def test_flush_clears_queue(self, orch, monkeypatch):
        """After flush, _pending_done_notices[project] is removed."""
        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        orch._pending_done_notices[proj] = [
            {"role": "frontend", "note": "done", "body": "[frontend done] done"}
        ]

        orch._flush_pending_done_notices(proj)

        assert proj not in orch._pending_done_notices

    def test_flush_noop_when_lead_dead(self, orch, monkeypatch):
        """Flush is a no-op when Lead is not alive — queue preserved."""
        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        orch._pending_done_notices[proj] = [
            {"role": "devops", "note": "deployed", "body": "[devops done] deployed"}
        ]

        orch._flush_pending_done_notices(proj)

        assert len(orch._pending_done_notices.get(proj, [])) == 1

    def test_flush_noop_when_nothing_pending(self, orch, monkeypatch):
        """Flush with empty queue makes no writes."""
        proj = "proj_b"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)

        orch._flush_pending_done_notices(proj)

        lead_session.write.assert_not_called()

    def test_flush_logs_done_notices_flushed(self, orch, tmp_path, monkeypatch):
        """_flush_pending_done_notices writes done_notices_flushed to events.log."""
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)

        proj = "proj_b"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        orch._pending_done_notices[proj] = [
            {"role": "reviewer", "note": "lgtm", "body": "[reviewer done] lgtm"},
        ]

        orch._flush_pending_done_notices(proj)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        flushed = [e for e in events if e["event"] == "done_notices_flushed"]
        assert len(flushed) == 1
        assert flushed[0]["project"] == proj
        assert flushed[0]["count"] == 1

    def test_flush_only_target_project(self, orch, monkeypatch):
        """Flushing proj_b queue doesn't touch proj_c's queue."""
        for proj in ("proj_b", "proj_c"):
            _register_pane(orch, LEAD.name, proj, _make_alive_session())
            orch._pending_done_notices[proj] = [
                {"role": "backend", "note": "done", "body": f"[backend done] {proj}"}
            ]

        orch._flush_pending_done_notices("proj_b")

        assert "proj_b" not in orch._pending_done_notices
        assert len(orch._pending_done_notices.get("proj_c", [])) == 1


# ─────────────────────────────────────────────────────────────
# #13 — done-notice durability across cockpit restart
# ─────────────────────────────────────────────────────────────


class TestDoneNoticeDiskPersistence:
    """A teammate's done notice queued while Lead is down must survive a
    cockpit restart, mirroring the CC queue's disk persistence."""

    def test_save_then_load_round_trip(self, orch, tmp_path):
        orch._pending_done_notices = {
            "proj_x": [{"role": "backend", "note": "done", "body": "the body"}]
        }
        orch._save_pending_done_notices("proj_x")
        assert (tmp_path / "pending-done-notices-proj_x.json").exists()

        # Simulate restart: in-memory queue gone, reload from disk.
        orch._pending_done_notices = {}
        orch._load_pending_done_notices()
        assert orch._pending_done_notices["proj_x"][0]["note"] == "done"

    def test_save_empty_queue_removes_file(self, orch, tmp_path):
        path = tmp_path / "pending-done-notices-proj_y.json"
        orch._pending_done_notices = {"proj_y": [{"role": "qa", "note": "x", "body": "y"}]}
        orch._save_pending_done_notices("proj_y")
        assert path.exists()

        # Draining the queue must delete the file, not leave a stale [].
        orch._pending_done_notices["proj_y"] = []
        orch._save_pending_done_notices("proj_y")
        assert not path.exists()

    def test_queue_on_done_persists_to_disk(self, orch, tmp_path, monkeypatch):
        # End-to-end: done() while Lead absent both queues in memory AND writes
        # the durable file so a restart can recover it.
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj_z", {}))
        _mock_done(orch)
        _register_pane(orch, LEAD.name, "proj_z", _make_dead_session())
        _register_pane(orch, "backend", "proj_z", _make_alive_session())

        orch.done("backend", note="finished the endpoint", project="proj_z")

        assert (tmp_path / "pending-done-notices-proj_z.json").exists()
        assert len(orch._pending_done_notices.get("proj_z", [])) == 1


class TestVerifyFailHandoffSuggestion:
    """Tier 2c: the fix-loop proposal carries a signature-based role suggestion."""

    def test_backend_signature_suggested(self):
        from agent_takkub.orchestrator import Orchestrator

        msg = Orchestrator._build_verify_fail_handoff(
            "qa", "login fail: POST /auth 500 traceback in api log"
        )
        assert "[qa FAILED]" in msg
        assert "**backend**" in msg  # suggestion present
        assert "propose-then-fire" in msg  # doctrine unchanged

    def test_unknown_signature_no_suggestion_line(self):
        from agent_takkub.orchestrator import Orchestrator

        msg = Orchestrator._build_verify_fail_handoff("qa", "ผลไม่ตรง spec")
        assert "[qa FAILED]" in msg
        assert "signature ชี้" not in msg  # falls back to manual diagnosis flow
