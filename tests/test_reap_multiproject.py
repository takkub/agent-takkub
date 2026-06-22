"""Repro for #70 — done-notice spill not reaped when >1 project active.

Real-world: backend+gemini of project A finished + sent done, but the notices
spilled to _pending_done_notices and were never reaped back to A's Lead while a
second project B was active (B got flushed, A starved) → A's Lead chain stalled.

These tests pin the reaper's multi-project behaviour so we can tell a logic bug
from a runtime is_at_ready_prompt() issue.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _lead(*, ready: bool = True) -> MagicMock:
    pane = MagicMock()
    s = MagicMock()
    s.is_alive = True
    s.is_at_ready_prompt = MagicMock(return_value=ready)
    s.write = MagicMock()
    pane.session = s
    return pane


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda project: project))
    monkeypatch.setattr(Orchestrator, "_save_pending_done_notices", lambda self, p: None)
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _written(session: MagicMock) -> str:
    parts = []
    for c in session.write.call_args_list:
        a = c.args[0] if c.args else ""
        parts.append(a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a))
    return "".join(parts)


def _drain(orch: Orchestrator, project: str) -> None:
    for _ in range(100):
        q = getattr(orch, "_lead_notify_queue", {}).get(project)
        if not q:
            break
        orch._pump_lead_notify(project)


def test_reap_flushes_all_projects_when_both_ready(orch: Orchestrator) -> None:
    """Both projects pending + both Leads ready → BOTH must flush (the #70 bug:
    only one flushed)."""
    a, b = _lead(ready=True), _lead(ready=True)
    orch._panes_by_project["projA"] = {"lead": a}
    orch._panes_by_project["projB"] = {"lead": b}
    orch._pending_done_notices = {
        "projA": [{"role": "backend", "note": "n", "body": "A-DONE"}],
        "projB": [{"role": "qa", "note": "n", "body": "B-DONE"}],
    }

    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()
        _drain(orch, "projA")
        _drain(orch, "projB")

    assert "A-DONE" in _written(a.session), "project A starved (the #70 bug)"
    assert "B-DONE" in _written(b.session)


def test_busy_project_not_lost_then_flushes_when_ready(orch: Orchestrator) -> None:
    """A ready, B busy → A flushes now; B stays pending and flushes on a later
    tick once ready (must not be dropped)."""
    a, b = _lead(ready=True), _lead(ready=False)
    orch._panes_by_project["projA"] = {"lead": a}
    orch._panes_by_project["projB"] = {"lead": b}
    orch._pending_done_notices = {
        "projA": [{"role": "backend", "note": "n", "body": "A-DONE"}],
        "projB": [{"role": "qa", "note": "n", "body": "B-DONE"}],
    }

    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()
        _drain(orch, "projA")

    assert "A-DONE" in _written(a.session)
    assert "projB" in orch._pending_done_notices, "B must survive while busy"

    # Later tick: B becomes ready.
    b.session.is_at_ready_prompt.return_value = True
    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()
        _drain(orch, "projB")

    assert "B-DONE" in _written(b.session), "B never delivered after becoming ready"


def test_perpetually_not_ready_lead_force_flushes_after_stale(orch: Orchestrator) -> None:
    """The #70 root cause: Lead alive but is_at_ready_prompt() is a perpetual
    false-negative (blocker marker in its visible conversation reads as busy).
    The reaper must not strand the notices forever — after _DONE_NOTICE_STALE_S
    it force-delivers regardless of the gate."""
    from agent_takkub.lead_inbox import _DONE_NOTICE_STALE_S

    a = _lead(ready=False)  # never reads ready
    orch._panes_by_project["projA"] = {"lead": a}
    orch._pending_done_notices = {"projA": [{"role": "backend", "note": "n", "body": "A-DONE"}]}

    # First tick: arms the staleness clock, must NOT deliver yet (could be a
    # genuinely-busy Lead mid-turn).
    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()
    assert "A-DONE" not in _written(a.session), "force-flushed too early"
    assert "projA" in orch._pending_done_notices
    assert "projA" in orch._pending_done_since

    # Simulate the stall window elapsing.
    orch._pending_done_since["projA"] -= _DONE_NOTICE_STALE_S + 1

    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()

    assert "A-DONE" in _written(a.session), "stale notice never force-delivered (#70)"
    assert "projA" not in orch._pending_done_notices
    assert "projA" not in orch._pending_done_since


def test_ready_flush_clears_staleness_clock(orch: Orchestrator) -> None:
    """A normal ready-path flush must clear the staleness clock so a later spill
    starts its own fresh window (no carry-over force-flush)."""
    a = _lead(ready=True)
    orch._panes_by_project["projA"] = {"lead": a}
    orch._pending_done_notices = {"projA": [{"role": "backend", "note": "n", "body": "A-DONE"}]}
    orch._pending_done_since = {"projA": 1.0}  # stale leftover from a prior window

    with patch("agent_takkub.orchestrator.QTimer.singleShot"):
        orch._reap_pending_done_notices()
        _drain(orch, "projA")

    assert "A-DONE" in _written(a.session)
    assert "projA" not in orch._pending_done_since, "staleness clock not cleared on flush"
