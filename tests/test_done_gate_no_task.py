"""Targeted tests for #278/#276 — `takkub done` reporting on a task that never
reached the pane must be refused.

Measured in #278: `codex exec "reply with the single word: ok"` ran
`takkub done "Acknowledged user request"` on its very first turn, having read
the injected orchestrator instructions and decided that answering the prompt
was the job. In the pane version of the same behaviour Lead received a
complete-looking report for a task that had never run — the target file still
held its original code. #276 is the same hole reached from the other side: a
pane wedged in its provider's boot phase never receives its task, so any
`done` it emits necessarily belongs to something else.

The gate asks "did the assigned task ever arrive?", never "was the work any
good" — the latter is not decidable here (the zero-file-change flag in
`digest_facts` warns about that instead, without blocking). A pane with no
assignment on record is left alone: `takkub spawn` plus instructions typed by
hand is a real flow, and with no assignment there is no task for a premature
`done` to close out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.spawn_engine import PaneState

PROJECT = "donegate"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _pane(state: str = "active") -> MagicMock:
    p = MagicMock()
    p.state = state
    p.session = MagicMock()
    p.session.is_alive = True
    return p


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {PROJECT: {"backend": _pane()}}
    o._pane_state = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestUndeliveredTaskGate:
    def test_assigned_but_undelivered_task_is_refused(self, orch: Orchestrator) -> None:
        """The #278 shape: Lead dispatched a task, the pane fired `done`
        before it ever arrived."""
        orch._pane_state[f"{PROJECT}::backend"] = PaneState(
            assign_ts=1.0, last_assigned_task="build the reset flow"
        )
        ok, msg = orch.done("backend", note="Acknowledged user request", project=PROJECT)
        assert ok is False
        assert "ยังไม่เคยถูกส่งถึง pane" in msg
        assert "--force" in msg

    def test_pane_with_no_assignment_is_left_alone(self, orch: Orchestrator) -> None:
        """`takkub spawn` + instructions typed by hand is a real flow the
        cockpit keeps no record of — and with no assignment on record there is
        no task a premature done could close out."""
        orch._pane_state[f"{PROJECT}::backend"] = PaneState()
        assert orch._pane_reports_undelivered_task(PROJECT, "backend", _pane()) is False

    def test_delivered_task_is_accepted(self, orch: Orchestrator, monkeypatch) -> None:
        orch._pane_state[f"{PROJECT}::backend"] = PaneState(assign_ts=1.0, last_assigned_task="t")
        orch._last_delivery_ids = {(PROJECT, "backend"): "dlv-1"}
        assert orch._pane_reports_undelivered_task(PROJECT, "backend", _pane()) is False

    def test_preloaded_spawn_task_counts_as_received(self, orch: Orchestrator) -> None:
        orch._pane_state[f"{PROJECT}::backend"] = PaneState(
            assign_ts=1.0, spawn_initial_task_state="delivered"
        )
        assert orch._pane_reports_undelivered_task(PROJECT, "backend", _pane()) is False

    def test_takkub_send_counts_as_received(self, orch: Orchestrator) -> None:
        orch._pane_state[f"{PROJECT}::backend"] = PaneState(assign_ts=1.0, last_send_ts=123.0)
        assert orch._pane_reports_undelivered_task(PROJECT, "backend", _pane()) is False

    def test_working_pane_is_never_refused(self, orch: Orchestrator) -> None:
        """Catch-all: a state this gate did not model must not cost a real
        report. If the orchestrator itself says the pane is working, it is."""
        orch._pane_state[f"{PROJECT}::backend"] = PaneState(assign_ts=1.0, last_assigned_task="t")
        assert orch._pane_reports_undelivered_task(PROJECT, "backend", _pane("working")) is False

    def test_force_bypasses_the_gate(self, orch: Orchestrator, monkeypatch) -> None:
        """The legitimate manual case: a pane spawned by hand and driven in its
        own terminal, where the cockpit genuinely has no record of the work."""
        monkeypatch.setattr(orch, "_pane_reports_undelivered_task", lambda *a, **k: True)
        try:
            _ok, msg = orch.done("backend", note="manual", project=PROJECT, force=True)
        except Exception:
            # done() continues into real teardown this minimal fixture can't
            # support — getting that far already proves the gate was skipped.
            return
        assert "ยังไม่เคยถูกส่งถึง pane" not in msg, "force must bypass the #278 gate"


class TestZeroFileChangeIsFlagged:
    def test_measured_zero_files_is_marked(self) -> None:
        from agent_takkub.digest_facts import DigestFacts, format_digest_fact_line

        line = format_digest_fact_line(DigestFacts(role="frontend", files_touched=0))
        assert "⚠️" in line
        assert "ยังไม่มีอะไรเปลี่ยน" in line

    def test_unverifiable_is_not_dressed_up_as_zero(self) -> None:
        from agent_takkub.digest_facts import DigestFacts, format_digest_fact_line

        line = format_digest_fact_line(DigestFacts(role="frontend", files_touched=None))
        assert "ตรวจไม่ได้" in line
        assert "ยังไม่มีอะไรเปลี่ยน" not in line

    def test_real_changes_are_unmarked(self) -> None:
        from agent_takkub.digest_facts import DigestFacts, format_digest_fact_line

        line = format_digest_fact_line(DigestFacts(role="frontend", files_touched=3))
        assert "ไฟล์ที่แตะ:3" in line
        assert "⚠️ ไฟล์ที่แตะ" not in line
