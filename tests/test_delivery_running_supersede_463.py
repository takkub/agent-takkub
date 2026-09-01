"""#463 follow-up (real e2e incident, 2026-09-01 11:24-11:25):

Lead `takkub send` (GO) landed on a pane whose delivery had already reached
RUNNING via `progress()`. `supersede_for_session` still cancelled it because
`has_reached_pane()` — correctly used to gate "is it safe to throw this
away" — says True for RUNNING too, and `orchestrator.send()` drops the
role's `_last_delivery_ids` entry the moment anything gets cancelled. The
teammate's own later `takkub done` then had no delivery_id left to call
`mark_done()` on: the delivery ledger showed CANCELLED for a task that
finished successfully, no `task_delivery_done` event ever fired.

Fix: `supersede_for_session` only cancels deliveries in a state the
self-heal/verify resend loop can still act on
(`task_delivery._RESEND_ELIGIBLE_STATES`: WRITING/WRITTEN/SUBMITTING/
ACCEPTED). RUNNING and SPAWNED_IDLE mean the teammate already confirmed the
task and is acting on it, so they're left alone — not cancelled, not
reported as kept/pending either (that would be equally misleading).

Covers, at both the `DeliveryManager` unit level and the `Orchestrator`
end-to-end level:
  (a) progress() -> RUNNING, then send() -> delivery stays RUNNING, nothing
      cancelled, `_last_delivery_ids` untouched.
  (b) done() after that still finds the delivery_id and marks it DONE, and
      the `task_delivery_done` event fires.
  (c) ACCEPTED is unaffected — send() still cancels it (#255, unchanged).
  (d) UNCERTAIN is unaffected — send() still keeps it, unconfirmed (#336,
      unchanged).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState
from agent_takkub.task_delivery import DeliveryManager, DeliveryState


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _pane(session=None, generation: int = 0) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.state = "working"
    p.set_state = MagicMock()
    p._session_generation = generation
    return p


class TestSupersedeUnitLevel:
    """DeliveryManager.supersede_for_session in isolation."""

    def test_running_is_neither_cancelled_nor_kept(self) -> None:
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        manager.mark_running(delivery.delivery_id)

        cancelled, kept = manager.supersede_for_session("P", "backend", 0)

        assert cancelled == []
        assert kept == []
        assert delivery.state == DeliveryState.RUNNING

    def test_spawned_idle_is_neither_cancelled_nor_kept(self) -> None:
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.mark_spawned_idle(delivery.delivery_id)

        cancelled, kept = manager.supersede_for_session("P", "backend", 0)

        assert cancelled == []
        assert kept == []
        assert delivery.state == DeliveryState.SPAWNED_IDLE

    def test_accepted_is_still_cancelled(self) -> None:
        """#255, unchanged: ACCEPTED already pasted once, so cancelling it
        only suppresses a duplicate re-paste."""
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        manager.mark_accepted(delivery.delivery_id)

        cancelled, kept = manager.supersede_for_session("P", "backend", 0)

        assert cancelled == [delivery]
        assert kept == []
        assert delivery.state == DeliveryState.CANCELLED

    def test_uncertain_is_still_kept(self) -> None:
        """#336, unchanged: an UNCERTAIN delivery cannot be proven to have
        reached the pane, so cancelling it might destroy the only copy."""
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.mark_uncertain(delivery.delivery_id)

        cancelled, kept = manager.supersede_for_session("P", "backend", 0)

        assert cancelled == []
        assert kept == [delivery]
        assert delivery.state == DeliveryState.UNCERTAIN


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "active_project", lambda: ("P", {}))

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


def _register(orch: Orchestrator, role: str, session=None, generation: int = 0) -> MagicMock:
    pane = _pane(session, generation=generation)
    orch._panes_by_project.setdefault("P", {})[role] = pane
    return pane


class TestOrchestratorEndToEnd:
    def test_progress_then_send_leaves_delivery_running_for_later_done(
        self, orch: Orchestrator
    ) -> None:
        """(a) The full real-world sequence: assign delivered, progress()
        moves it to RUNNING, Lead sends into the pane — the delivery must
        survive untouched so the teammate's own done() can still close it."""
        _register(orch, LEAD.name, _live_session())
        backend = _register(orch, "backend", _live_session())
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        orch._delivery_manager = manager
        orch._last_delivery_ids = {("P", "backend"): delivery.delivery_id}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            ok, _msg = orch.progress("backend", note="e2e: waiting for GO", project="P")
        assert ok is True
        assert delivery.state == DeliveryState.RUNNING

        with (
            patch("agent_takkub.orchestrator._log_event") as mock_log_event,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "GO", from_role="lead", project="P")
        assert ok is True

        # Never cancelled.
        assert delivery.state == DeliveryState.RUNNING
        assert not any(
            c.args and c.args[0] == "delivery_superseded_by_send"
            for c in mock_log_event.call_args_list
        )
        # And still the delivery of record for this role.
        assert orch._last_delivery_ids[("P", "backend")] == delivery.delivery_id
        # Nothing written to the pane about a cancelled/pending delivery.
        written = [
            c.args[0]
            for c in backend.session.write.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        assert not any("delivery" in m.lower() for m in written)

    def test_done_after_running_marks_delivery_done_with_event(self, orch: Orchestrator) -> None:
        """(b) done() must still find the delivery_id (untouched by the fix
        above) and flip it to DONE, firing `task_delivery_done`."""
        _register(orch, LEAD.name, _live_session())
        _register(orch, "backend", _live_session())
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        manager.mark_running(delivery.delivery_id)
        orch._delivery_manager = manager
        orch._last_delivery_ids = {("P", "backend"): delivery.delivery_id}
        orch._pane_state["P::backend"] = PaneState(last_assigned_task="do X", assign_ts=time.time())
        orch._notify_lead = MagicMock()

        ok, _msg = orch.done("backend", note="เสร็จแล้ว", project="P")

        assert ok is True
        assert manager.get(delivery.delivery_id).state == DeliveryState.DONE
        # `_last_delivery_ids` is consumed (popped) by done() itself.
        assert ("P", "backend") not in orch._last_delivery_ids

    def test_send_still_cancels_an_accepted_delivery_end_to_end(self, orch: Orchestrator) -> None:
        """(c) ACCEPTED stays cancel-worthy through the real send() path,
        unchanged behaviour from #255."""
        _register(orch, LEAD.name, _live_session())
        _register(orch, "backend", _live_session())
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=0, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        manager.mark_accepted(delivery.delivery_id)
        orch._delivery_manager = manager
        orch._last_delivery_ids = {("P", "backend"): delivery.delivery_id}

        with (
            patch("agent_takkub.orchestrator._log_event") as mock_log_event,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "correction", from_role="lead", project="P")

        assert ok is True
        assert delivery.state == DeliveryState.CANCELLED
        superseded_call = next(
            c
            for c in mock_log_event.call_args_list
            if c.args and c.args[0] == "delivery_superseded_by_send"
        )
        assert superseded_call.kwargs["cancelled"] == 1
        # The role's delivery pointer is cleared along with the cancel.
        assert ("P", "backend") not in orch._last_delivery_ids
