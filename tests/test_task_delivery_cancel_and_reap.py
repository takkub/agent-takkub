"""Tests for issue #255: Lead-facing controls over a stuck task delivery.

Covers:
  - `Orchestrator.cancel_task_delivery` (the `takkub task cancel --role <r>`
    CLI target) — cancels a pending delivery directly instead of requiring
    close()+reassign.
  - `send()` auto-cancelling a stale delivery when Lead sends straight into
    a pane that still has one retrying (the #255 duplicate-paste race).
  - `Orchestrator._reap_stale_deliveries` (wired into the idle watchdog
    tick) sweeping in-flight deliveries past their TTL and notifying Lead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.task_delivery import DeliveryManager


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session(generation: int = 1) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _pane(session=None, generation: int = 1) -> MagicMock:
    p = MagicMock()
    p.session = session
    p._session_generation = generation
    return p


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestCancelTaskDelivery:
    def test_cancels_pending_delivery_for_current_generation(self, orch: Orchestrator) -> None:
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"backend": backend}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager

        ok, msg = orch.cancel_task_delivery("backend", project="P")

        assert ok is True
        assert "cancelled 1" in msg
        assert delivery.state.value == "cancelled"

    def test_no_pending_delivery_is_still_ok(self, orch: Orchestrator) -> None:
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"backend": backend}
        orch._delivery_manager = DeliveryManager(default_ttl_sec=120)

        ok, msg = orch.cancel_task_delivery("backend", project="P")

        assert ok is True
        assert "no pending delivery" in msg

    def test_unknown_role_fails(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {}
        ok, _msg = orch.cancel_task_delivery("ghost", project="P")
        assert ok is False

    def test_no_delivery_manager_yet_is_ok_noop(self, orch: Orchestrator) -> None:
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"backend": backend}
        # _delivery_manager never initialised (nothing delivered yet).
        ok, msg = orch.cancel_task_delivery("backend", project="P")
        assert ok is True
        assert "no pending delivery" in msg


class TestSendAutoCancelsStaleDelivery:
    def test_lead_send_cancels_delivery_that_already_reached_the_pane(
        self, orch: Orchestrator
    ) -> None:
        """#255's actual case: the task was pasted once already, so the only
        thing cancelling suppresses is an idempotent re-paste."""
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        manager.begin_write(delivery.delivery_id, 1)
        manager.mark_written(delivery.delivery_id)
        orch._delivery_manager = manager
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send(
                "backend", "hey, doing this myself now", from_role="lead", project="P"
            )

        assert ok is True
        assert delivery.state.value == "cancelled"
        notices = _written_strings(lead.session)
        assert any("[delivery-superseded]" in m and "#255" in m for m in notices)

    def test_lead_send_keeps_a_delivery_that_never_reached_the_pane(
        self, orch: Orchestrator
    ) -> None:
        """#295: a QUEUED delivery is the pane's ONLY copy of its task.

        Field case: `assign` at 11:35:25 then `send` at 11:35:39 — the old
        code cancelled the brand-new assignment and reported it with the same
        wording used for a harmless stale re-paste, so Lead could not tell
        whether the teammate still had work. Under unattended overnight
        operation that pane goes silent until morning.
        """
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1abc999",
            project_id="P",
            pane_id="backend",
            session_generation=1,
            payload="do X",
        )
        orch._delivery_manager = manager
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "correction: use src/", from_role="lead", project="P")

        assert ok is True
        # Still armed — the task must still land, or the pane has no work.
        assert delivery.state.value == "queued"
        notices = _written_strings(lead.session)
        assert any("[delivery-pending]" in m for m in notices)
        # And Lead is NOT told something was superseded, which would read as
        # "safe, nothing to do".
        assert not any("[delivery-superseded]" in m for m in notices)

    def test_lead_send_keeps_a_delivery_whose_arrival_is_uncertain(
        self, orch: Orchestrator
    ) -> None:
        """#336: UNCERTAIN means the paste went out but the confirmation came
        back ambiguous — it is NOT proof the pane got the task.

        Field case: gemini went UNCERTAIN at 15:48:33, a `send` superseded it
        at 15:49:14, and its transcript never contained the task at all —
        while Lead was told the cancellation was "ปลอดภัย ไม่ต้องทำอะไร" and
        `takkub status` reported `working` for four more minutes.
        """
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t9def111",
            project_id="P",
            pane_id="backend",
            session_generation=1,
            payload="do X",
        )
        manager.begin_write(delivery.delivery_id, 1)
        manager.mark_written(delivery.delivery_id)
        manager.mark_uncertain(delivery.delivery_id)
        orch._delivery_manager = manager
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "แก้ข้อ 3 ด้วย", from_role="lead", project="P")

        assert ok is True
        assert delivery.state.value == "uncertain"
        notices = _written_strings(lead.session)
        assert any("[delivery-unconfirmed]" in m for m in notices)
        # Never the "safe, nothing to do" wording — that was the whole bug.
        assert not any("[delivery-superseded]" in m for m in notices)
        # Nor the plain pending wording, which promises it will land on its
        # own; an uncertain delivery is not re-pasted (#134/#328).
        assert not any("[delivery-pending]" in m for m in notices)

    def test_explicit_cancel_verb_still_cancels_an_undelivered_delivery(
        self, orch: Orchestrator
    ) -> None:
        """`takkub cancel` states the intent outright — unlike `send`, it must
        drop the delivery however far it got (#295 must not weaken it)."""
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"backend": backend}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager

        ok, msg = orch.cancel_task_delivery("backend", project="P")

        assert ok is True
        assert "cancelled 1" in msg
        assert delivery.state.value == "cancelled"

    def test_peer_send_does_not_cancel_delivery(self, orch: Orchestrator) -> None:
        """A teammate messaging another teammate (from_role != lead/None)
        must never cancel an in-flight delivery — only Lead recovering."""
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        qa = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend, "qa": qa}
        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch.send("backend", "fyi", from_role="qa", project="P")

        assert delivery.state.value != "cancelled"

    def test_send_with_no_delivery_manager_is_unaffected(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "hi", from_role="lead", project="P")
        assert ok is True  # must not raise / must not require _delivery_manager to exist


class TestReapStaleDeliveries:
    def test_reaps_in_flight_delivery_and_notifies_lead(self, orch: Orchestrator) -> None:
        now = [100.0]
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
        manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager
        now[0] = 200.0

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._reap_stale_deliveries()

        notices = _written_strings(lead.session)
        assert any("[delivery-stale-reap]" in m and "backend" in m and "#255" in m for m in notices)

    def test_noop_when_nothing_stale(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        orch._delivery_manager = DeliveryManager(default_ttl_sec=120)

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._reap_stale_deliveries()

        assert not _written_strings(lead.session)

    def test_noop_without_delivery_manager(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {}
        orch._reap_stale_deliveries()  # must not raise

    def test_suppressed_when_pane_is_working_with_recent_output(self, orch: Orchestrator) -> None:
        """#359: a long task paste can outlast the fixed delivery TTL to
        ingest even though it genuinely landed — if the pane is visibly
        working with recent output, don't tell Lead to reassign it."""
        now = [100.0]
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        backend.state = "working"
        backend.session.seconds_since_output.return_value = 2.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
        manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager
        now[0] = 200.0

        with patch("agent_takkub.lead_inbox._log_event") as log_event:
            orch._reap_stale_deliveries()

        assert not _written_strings(lead.session)
        assert any(
            c.args and c.args[0] == "delivery_stale_reap_suppressed" for c in log_event.mock_calls
        )

    def test_still_reaps_when_pane_quiet_a_while(self, orch: Orchestrator) -> None:
        """'working' state alone isn't enough — recent output is required,
        or a pane wedged in 'working' forever would mask a genuinely stuck
        delivery."""
        now = [100.0]
        lead = _pane(_live_session())
        backend = _pane(_live_session(), generation=1)
        backend.state = "working"
        backend.session.seconds_since_output.return_value = 999.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
        manager.create(
            task_id="t1", project_id="P", pane_id="backend", session_generation=1, payload="do X"
        )
        orch._delivery_manager = manager
        now[0] = 200.0

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._reap_stale_deliveries()

        notices = _written_strings(lead.session)
        assert any("[delivery-stale-reap]" in m and "backend" in m for m in notices)
