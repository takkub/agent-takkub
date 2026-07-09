"""Integration tests for the Lead draft-typing guard (issue #3).

Covers the engine-side wiring on top of the pure state machine
(test_lead_draft_state.py):
  - Orchestrator._on_pane_input feeds Lead-pane keystrokes into the tracker,
    routed by project (not role-name lookup) via _project_ns_for_pane.
  - Non-Lead pane input never touches the draft tracker.
  - Engine-originated writes (e.g. _notify_lead's own session.write) never
    feed the tracker, since they bypass _on_pane_input entirely.
  - _pump_lead_notify holds delivery while a draft is pending, delivers once
    it clears, and spills to the durable queue once the hold times out.
  - _flush_pending_lead_cc holds while a draft is pending.
  - inject_slash_command_when_ready holds for the Lead role but is unaffected
    for non-Lead roles.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.agent_pane import AgentPane
from agent_takkub.lead_draft_state import DRAFT_HOLD_TIMEOUT_S, NONEMPTY, LeadDraftState
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "drafttest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_lead_session(*, ready: bool = True) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.is_at_ready_prompt = MagicMock(return_value=ready)
    s.write = MagicMock()
    return s


def _make_pane(role_name: str, *, ready: bool = True) -> MagicMock:
    pane = MagicMock()
    pane.role.name = role_name
    pane.session = _make_lead_session(ready=ready)
    return pane


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class TestOnPaneInputRouting:
    """_on_pane_input must route Lead keystrokes into the right project's
    draft tracker by pane identity, not role-name, and leave non-Lead panes
    untouched."""

    def test_lead_pane_input_feeds_draft_tracker(self, orch: Orchestrator) -> None:
        pane = _make_pane("lead")
        orch._panes_by_project[TEST_PROJECT] = {"lead": pane}

        orch._on_pane_input("lead", b"hello")

        state = orch._lead_draft_state.get(TEST_PROJECT)
        assert state is not None
        assert state.state == NONEMPTY
        assert state.draft_len == 5
        pane.session.write.assert_called_once_with(b"hello")

    def test_non_lead_pane_input_does_not_touch_draft_tracker(self, orch: Orchestrator) -> None:
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        orch._on_pane_input("backend", b"hello")

        assert getattr(orch, "_lead_draft_state", {}) == {}
        pane.session.write.assert_called_once_with(b"hello")

    def test_routes_to_the_correct_project_among_several(self, orch: Orchestrator) -> None:
        """Both projects' Lead panes share role.name == 'lead' — routing must
        follow sender() identity, not the role-name fallback (which only
        ever sees the *active* project)."""
        pane_a = _make_pane("lead")
        pane_b = _make_pane("lead")
        pane_a.__class__ = AgentPane
        pane_b.__class__ = AgentPane
        orch._panes_by_project["proj-a"] = {"lead": pane_a}
        orch._panes_by_project["proj-b"] = {"lead": pane_b}

        # Simulate the Qt signal-emission context: sender() returns the
        # AgentPane instance that actually emitted the keystrokes.
        orch.sender = lambda: pane_b
        orch._on_pane_input("lead", b"x")

        assert "proj-b" in orch._lead_draft_state
        assert "proj-a" not in orch._lead_draft_state
        pane_b.session.write.assert_called_once_with(b"x")
        pane_a.session.write.assert_not_called()

    def test_engine_originated_writes_never_feed_tracker(self, orch: Orchestrator) -> None:
        """_notify_lead writes directly to session.write(), bypassing
        _on_pane_input entirely — it must never move the draft tracker."""
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "an engine-originated notice")

        assert lead.session.write.called
        assert getattr(orch, "_lead_draft_state", {}).get(TEST_PROJECT) is None


class TestProjectNsForPane:
    def test_finds_owning_project_by_identity(self, orch: Orchestrator) -> None:
        pane_a = _make_pane("lead")
        pane_b = _make_pane("lead")
        orch._panes_by_project["proj-a"] = {"lead": pane_a}
        orch._panes_by_project["proj-b"] = {"lead": pane_b}

        assert orch._project_ns_for_pane(pane_a) == "proj-a"
        assert orch._project_ns_for_pane(pane_b) == "proj-b"

    def test_returns_none_for_unregistered_pane(self, orch: Orchestrator) -> None:
        orphan = _make_pane("lead")
        assert orch._project_ns_for_pane(orphan) is None


class TestPumpLeadNotifyDraftGuard:
    def test_holds_delivery_while_draft_pending(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=time.time()
        )

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "done notice")

        lead.session.write.assert_not_called()
        q = orch._lead_notify_queue.get(TEST_PROJECT)
        assert q and len(q) == 1

    def test_delivers_once_draft_clears(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=time.time()
        )

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "done notice")
        lead.session.write.assert_not_called()

        # User submits their draft -> tracker clears (simulates the Enter
        # keystroke flowing through _on_pane_input).
        orch._on_pane_input("lead", b"\r")
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        assert lead.session.write.called

    def test_hold_expired_spills_to_durable_queue(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        stale_ts = time.time() - DRAFT_HOLD_TIMEOUT_S - 1
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=stale_ts
        )

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "stale notice")

        lead.session.write.assert_not_called()
        q = orch._lead_notify_queue.get(TEST_PROJECT)
        assert not q, "in-memory queue must be drained after the spill"
        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        assert any("stale notice" in item["body"] for item in durable)
        assert any(item["note"] == "notify_draft_spill" for item in durable)

    def test_draft_guard_does_not_hold_when_lead_is_busy_typing_elsewhere(
        self, orch: Orchestrator
    ) -> None:
        """A draft in a DIFFERENT project must not hold this project's pump."""
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._lead_draft_state["other-project"] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=time.time()
        )

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice")

        assert lead.session.write.called


class TestFlushPendingLeadCcDraftGuard:
    def test_gate_blocks_flush_while_draft_pending(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._pending_lead_cc[TEST_PROJECT] = [{"body": "cc msg"}]
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=1, pending_since=time.time()
        )

        orch._flush_pending_lead_cc(TEST_PROJECT)

        lead.session.write.assert_not_called()
        assert orch._pending_lead_cc.get(TEST_PROJECT) == [{"body": "cc msg"}]

    def test_flush_proceeds_when_draft_empty(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._pending_lead_cc[TEST_PROJECT] = [{"body": "cc msg"}]

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._flush_pending_lead_cc(TEST_PROJECT)

        assert lead.session.write.called
        assert TEST_PROJECT not in orch._pending_lead_cc


class TestInjectSlashCommandDraftGuard:
    def test_holds_for_lead_role_while_draft_pending(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=time.time()
        )

        timers: list[tuple[int, object]] = []

        def capture(ms, fn):
            timers.append((ms, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=capture):
            orch.inject_slash_command_when_ready("lead", "/remote-control", project=TEST_PROJECT)
            # Fire the initial 1_500ms scheduling timer to run the first _check().
            assert timers
            timers[0][1]()

        lead.session.write.assert_not_called()

    def test_delivers_for_lead_role_once_draft_clears(self, orch: Orchestrator) -> None:
        lead = _make_pane("lead", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        timers: list[tuple[int, object]] = []

        def capture(ms, fn):
            timers.append((ms, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=capture):
            orch.inject_slash_command_when_ready("lead", "/remote-control", project=TEST_PROJECT)
            timers[0][1]()

        assert lead.session.write.called

    def test_guard_does_not_affect_non_lead_roles(self, orch: Orchestrator) -> None:
        """A non-Lead pane has no draft tracker fed for it — the guard must
        be a no-op there so teammate slash-injection is unaffected."""
        teammate = _make_pane("backend", ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"backend": teammate}
        # Even if some stale/foreign draft state exists under this project
        # namespace, a non-Lead role_name must bypass the check entirely.
        orch._lead_draft_state[TEST_PROJECT] = LeadDraftState(
            state=NONEMPTY, draft_len=3, pending_since=time.time()
        )

        timers: list[tuple[int, object]] = []

        def capture(ms, fn):
            timers.append((ms, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=capture):
            orch.inject_slash_command_when_ready("backend", "/some-command", project=TEST_PROJECT)
            timers[0][1]()

        assert teammate.session.write.called
