"""Regression tests for #586 (Silent task cancellation upon `takkub send`)
and #585 metrics (scope classification, override rate, wall times, guard denies).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.maintenance import check_scope_effort
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
        o._last_delivery_ids = {}
        o.cwd = str(tmp_path)
    monkeypatch.setattr(o, "_write_hot_md", MagicMock())
    return o


def _register(orch: Orchestrator, role: str, session=None, generation: int = 0) -> MagicMock:
    pane = _pane(session, generation=generation)
    orch._panes_by_project.setdefault("P", {})[role] = pane
    return pane


class TestTaskDeliveryProtection586:
    """Task delivery (kind='task') is NEVER cancelled by takkub send."""

    def test_assign_followed_by_send_preserves_task_delivery(
        self, orch: Orchestrator, tmp_path
    ) -> None:
        _register(orch, LEAD.name, _live_session())
        _register(orch, "backend", _live_session())

        task_file = tmp_path / "task.md"
        task_file.write_text("Do task 1", encoding="utf-8")

        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t1",
            project_id="P",
            pane_id="backend",
            session_generation=0,
            payload="Do task 1",
            kind="task",
        )
        orch._delivery_manager = manager
        orch._last_delivery_ids[("P", "backend")] = delivery.delivery_id
        orch._pane_state["P::backend"] = PaneState(
            last_assigned_task="Do task 1",
            last_assigned_task_file=str(task_file),
            assign_ts=time.time(),
        )

        # Send while delivery is still queued
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "Follow up instruction", from_role="lead", project="P")

        assert ok is True
        # Delivery must remain QUEUED (not cancelled!)
        assert delivery.state == DeliveryState.QUEUED
        assert orch._last_delivery_ids[("P", "backend")] == delivery.delivery_id

        # Merged text in task file
        merged = task_file.read_text(encoding="utf-8")
        assert "Do task 1" in merged
        assert "Follow up instruction" in merged

    def test_accepted_task_delivery_not_cancelled_by_send(self, orch: Orchestrator) -> None:
        _register(orch, LEAD.name, _live_session())
        _register(orch, "backend", _live_session())

        manager = DeliveryManager(default_ttl_sec=120)
        delivery = manager.create(
            task_id="t2",
            project_id="P",
            pane_id="backend",
            session_generation=0,
            payload="Do task 2",
            kind="task",
        )
        manager.begin_write(delivery.delivery_id, 0)
        manager.mark_written(delivery.delivery_id)
        manager.begin_submit(delivery.delivery_id, 0)
        manager.mark_accepted(delivery.delivery_id)

        orch._delivery_manager = manager
        orch._last_delivery_ids[("P", "backend")] = delivery.delivery_id

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "Check this too", from_role="lead", project="P")

        assert ok is True
        assert delivery.state == DeliveryState.ACCEPTED
        assert orch._last_delivery_ids[("P", "backend")] == delivery.delivery_id

    def test_cancelled_send_notifies_lead_immediately(self, orch: Orchestrator) -> None:
        lead = _register(orch, LEAD.name, _live_session())
        _register(orch, "backend", _live_session())

        manager = DeliveryManager(default_ttl_sec=120)
        send_delivery = manager.create(
            task_id="s1",
            project_id="P",
            pane_id="backend",
            session_generation=0,
            payload="Original send message",
            kind="send",
        )
        manager.begin_write(send_delivery.delivery_id, 0)
        manager.mark_written(send_delivery.delivery_id)
        manager.begin_submit(send_delivery.delivery_id, 0)
        manager.mark_accepted(send_delivery.delivery_id)

        orch._delivery_manager = manager
        orch._last_delivery_ids[("P", "backend")] = send_delivery.delivery_id

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            ok, _msg = orch.send("backend", "Newer send message", from_role="lead", project="P")

        assert ok is True
        assert send_delivery.state == DeliveryState.CANCELLED

        # Check that Lead was notified about the cancellation
        written = [
            c.args[0]
            for c in lead.session.write.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        assert any("[delivery-cancelled]" in m for m in written)

    def test_messages_shows_cancelled_deliveries(self, orch: Orchestrator) -> None:
        manager = DeliveryManager(default_ttl_sec=120)
        manager.create(
            task_id="c1",
            project_id="P",
            pane_id="backend",
            session_generation=0,
            payload="cancelled 1",
            kind="send",
        )
        manager.cancel_for_session("P", "backend", 0)
        orch._delivery_manager = manager

        _ok, msg, _items = orch.role_message_log("backend", project="P")
        assert "delivery ถูกยกเลิก 1" in msg

    def test_inbox_reports_cancelled_deliveries(self, orch: Orchestrator) -> None:
        manager = DeliveryManager(default_ttl_sec=120)
        manager.create(
            task_id="c2",
            project_id="P",
            pane_id="backend",
            session_generation=0,
            payload="cancelled 2",
            kind="send",
        )
        manager.cancel_for_session("P", "backend", 0)
        orch._delivery_manager = manager

        items = orch.inbox_report(project="P")
        cancelled_items = [i for i in items if i.get("queue") == "cancelled"]
        assert len(cancelled_items) == 1
        assert cancelled_items[0]["role"] == "backend"


class TestScopeEffortMetrics585:
    """Test metrics calculation in check_scope_effort."""

    def test_check_scope_effort_aggregates_events(self, tmp_path) -> None:
        events_file = tmp_path / "events.log"
        now_ts = datetime(2026, 9, 13, 8, 0, 0)
        now_iso = now_ts.isoformat()

        records = [
            # Scope assigned: 2 tiny (1 auto, 1 lead), 1 normal (auto), 1 deep (lead)
            {
                "ts": now_iso,
                "event": "scope_assigned",
                "role": "backend",
                "scope": "tiny",
                "source": "auto",
            },
            {
                "ts": now_iso,
                "event": "scope_assigned",
                "role": "qa",
                "scope": "tiny",
                "source": "lead",
            },
            {
                "ts": now_iso,
                "event": "scope_assigned",
                "role": "frontend",
                "scope": "normal",
                "source": "auto",
            },
            {
                "ts": now_iso,
                "event": "scope_assigned",
                "role": "backend",
                "scope": "deep",
                "source": "lead",
            },
            # Override: 1 override (normal -> deep)
            {
                "ts": now_iso,
                "event": "scope_override",
                "role": "backend",
                "auto_scope": "normal",
                "chosen_scope": "deep",
            },
            # Guard denied
            {
                "ts": now_iso,
                "event": "guard_denied",
                "role": "backend",
                "rule": "scope_tiny:qa_gate",
            },
            {
                "ts": now_iso,
                "event": "guard_denied",
                "role": "lead",
                "rule": "lead_direct_edit:deep_category",
            },
            # Test files written
            {
                "ts": now_iso,
                "event": "test_files_written",
                "role": "backend",
                "count": 2,
                "scope": "tiny",
            },
            {
                "ts": now_iso,
                "event": "test_files_written",
                "role": "frontend",
                "count": 3,
                "scope": "normal",
            },
            # Task wall time
            {
                "ts": now_iso,
                "event": "task_wall_time",
                "role": "backend",
                "duration_s": 45.0,
                "scope": "tiny",
            },
            {
                "ts": now_iso,
                "event": "task_wall_time",
                "role": "frontend",
                "duration_s": 120.0,
                "scope": "normal",
            },
        ]

        with events_file.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

        check = check_scope_effort(events_file, since_hours=24.0, now=now_ts)

        assert check.status == "ok"
        assert check.key == "scope_effort"
        assert check.title == "Scope & effort (24h)"
        assert "4 task(s) assigned" in check.summary
        assert "override rate 25.0%" in check.summary
        assert "2 guard denials" in check.summary

        # Check data fields
        assert check.data["total_assigned"] == 4
        assert check.data["override_count"] == 1
        assert check.data["override_rate"] == 25.0
        assert check.data["test_files_written"] == 5
        assert check.data["guard_denied"] == {
            "scope_tiny:qa_gate": 1,
            "lead_direct_edit:deep_category": 1,
        }
        assert check.data["wall_time_by_scope"] == {"tiny": 45.0, "normal": 120.0}

        # Check details lines
        detail_text = "\n".join(check.details)
        assert "Scope assigned (4):" in detail_text
        assert "Lead override rate: 25.0% (1/4)" in detail_text
        assert "Task wall time:" in detail_text
        assert "Test files written: 5 file(s)" in detail_text
        assert "Guard denied:" in detail_text
