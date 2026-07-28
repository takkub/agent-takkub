"""Regression tests for #133 — fan-out spawns backlog the Qt event loop, and a
burst of already-scheduled submit-verify timers firing the instant it clears
was mistaken for real swallow evidence, repasting on top of a paste that was
simply still rendering. Two independent mechanisms are covered:

  1. `_delayed_enter_verified` defers a swallow verdict (instead of concluding
     one) while a live main-thread heartbeat probe reports the Qt event loop
     was recently backlogged — bounded, so a stuck probe can't wedge the chain.
  2. `_pump_lead_notify` / `_force_deliver_done_notices` never write into the
     Lead composer while a previous write's own verify chain is still
     in-flight (events.log proved two independent chains raced the same
     session — duplicate `remaining: 3` log entries within milliseconds).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, set_main_thread_heartbeat_probe

TEST_PROJECT = "fanouttest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_heartbeat_probe():
    """Every test starts from the default (never-stale) probe and restores it
    afterward so this file can't leak a fake probe into other test modules."""
    set_main_thread_heartbeat_probe(lambda: 0.0)
    yield
    set_main_thread_heartbeat_probe(lambda: 0.0)


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


# ---------------------------------------------------------------------------
# Mechanism 1: stall-aware deferral inside _delayed_enter_verified
# ---------------------------------------------------------------------------


class TestStallAwareDeferral:
    def _swallow_session(self) -> MagicMock:
        """A session that looks exactly like a genuine swallowed paste (#26):
        ready prompt never drops, box never shows the payload, no output since
        the paste, and the render-settle guard's own quiet-window check is
        already past (seconds_since_output large) — the ONLY thing standing
        between this and an immediate repaste is the stall-defer gate."""
        session = MagicMock()
        session.is_at_ready_prompt.return_value = True
        session.shows_pending_input.return_value = False
        session.seconds_since_output.return_value = 5.0
        session.last_output_monotonic.return_value = 100.0
        return session

    def test_defers_repaste_while_heartbeat_reports_recent_backlog(self, qapp) -> None:
        """While the probe reports the Qt event loop was just backlogged, the
        verify loop must reschedule instead of concluding a swallow — no
        repaste, no extra CR, budget untouched."""
        session = self._swallow_session()
        pane = _pane(session)

        from agent_takkub.lead_inbox import _delayed_enter_verified

        set_main_thread_heartbeat_probe(lambda: 2.0)  # well past the 0.5s gate

        timers: list = []

        def _capture(_ms, fn):
            timers.append(fn)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=_capture):
            _delayed_enter_verified(pane, session, 0, payload="task", content_fragment="task")
            # Fire the initial-enter timer, then the first verify tick.
            timers.pop(0)()  # -> writes initial CR, schedules _verify
            timers.pop(0)()  # -> _verify: stall gate should defer here

        # Only the initial CR was written — the stall gate must have deferred
        # the swallow decision instead of repasting.
        writes = [c.args[0] for c in session.write.call_args_list]
        assert writes == [b"\r"], f"must not repaste while stalled, got {writes!r}"
        # A follow-up verify was scheduled (the defer itself), not abandoned.
        assert timers, "stall-deferred verify must reschedule, not give up silently"

    def test_proceeds_normally_once_heartbeat_recovers(self, qapp) -> None:
        """Once the probe reports the heartbeat is fresh again, the SAME
        genuinely-swallowed paste must still repaste — the defer only delays
        the verdict, it never suppresses the #26 recovery."""
        session = self._swallow_session()
        pane = _pane(session)

        from agent_takkub.lead_inbox import _delayed_enter_verified

        probe_age = [2.0]  # stale at first
        set_main_thread_heartbeat_probe(lambda: probe_age[0])

        repastes: list[int] = []
        timers: list = []

        def _capture(_ms, fn):
            timers.append(fn)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=_capture):
            _delayed_enter_verified(
                pane,
                session,
                0,
                payload="task",
                content_fragment="task",
                on_repaste=repastes.append,
            )
            timers.pop(0)()  # initial CR
            timers.pop(0)()  # verify #1 — deferred (stale probe)
            probe_age[0] = 0.0  # heartbeat recovers
            while timers:
                timers.pop(0)()

        assert repastes, "swallow must still be recovered once the heartbeat is fresh again"
        writes = [c.args[0] for c in session.write.call_args_list]
        assert "task" in writes, "the repaste must actually re-send the payload"

    def test_stall_defer_is_bounded_not_infinite(self, qapp) -> None:
        """A probe that NEVER recovers must not wedge the chain forever — after
        _STALL_DEFER_MAX defers, normal swallow/resume logic must resume."""
        from agent_takkub.lead_inbox import _STALL_DEFER_MAX, _delayed_enter_verified

        session = self._swallow_session()
        pane = _pane(session)
        set_main_thread_heartbeat_probe(lambda: 99.0)  # permanently "stale"

        repastes: list[int] = []
        timers: list = []

        def _capture(_ms, fn):
            timers.append(fn)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=_capture):
            _delayed_enter_verified(
                pane,
                session,
                0,
                payload="task",
                content_fragment="task",
                on_repaste=repastes.append,
            )
            # initial CR + up to _STALL_DEFER_MAX defers + the eventual real
            # decision — a small, fixed number of drains, never unbounded.
            for _ in range(_STALL_DEFER_MAX + 5):
                if not timers:
                    break
                timers.pop(0)()

        assert repastes, "budget-exhausted stall-defer must fall through to the real decision"

    def test_genuine_swallow_with_no_stall_repastes_immediately(self, qapp) -> None:
        """Baseline: with the default (never-stale) probe, existing #26
        recovery behaviour is completely unchanged."""
        session = self._swallow_session()
        pane = _pane(session)

        from agent_takkub.lead_inbox import _delayed_enter_verified

        repastes: list[int] = []
        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=lambda _ms, fn: fn()):
            _delayed_enter_verified(
                pane,
                session,
                0,
                payload="task",
                content_fragment="task",
                on_repaste=repastes.append,
            )

        # Matches the existing test_render_lag_repaste_still_bounded baseline:
        # not < _RENDER_ACTIVE_S so the render-wait is skipped every round,
        # repasting immediately each time until max_resends is exhausted.
        assert repastes == [3, 2, 1]


# ---------------------------------------------------------------------------
# Mechanism 2: serialised writes into the Lead composer
# ---------------------------------------------------------------------------


def _make_lead_session(*, ready: bool = True) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.is_at_ready_prompt = MagicMock(return_value=ready)
    s.write = MagicMock()
    return s


def _make_lead_pane(*, ready: bool = True) -> MagicMock:
    pane = MagicMock()
    pane.session = _make_lead_session(ready=ready)
    return pane


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator, "_resolve_project", staticmethod(lambda project: project or TEST_PROJECT)
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class TestLeadWriteSerialisation:
    def test_next_item_not_written_while_previous_chain_still_verifying(
        self, orch: Orchestrator
    ) -> None:
        """Two notices queued; Lead ready the whole time (the exact ambiguous
        state that let two chains race in production). Once the pump delivers
        item 1 and starts its verify chain, a second pump entry (standing in
        for the real event-loop re-driving it) must NOT write item 2 while
        that chain is still unsettled."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice A")
            orch._notify_lead(TEST_PROJECT, "notice B")

        # notice A already delivered synchronously by the first pump attempt
        # inside _notify_lead's _arm_lead_notify_pump; its chain is in flight.
        assert TEST_PROJECT in orch._lead_notify_verify_active
        write_count_after_a = lead.session.write.call_count
        assert write_count_after_a >= 1

        # Re-entering the pump (simulating the "next pump" timer firing) must
        # not advance to item B while the guard is set.
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        assert lead.session.write.call_count == write_count_after_a, (
            "must not write a second item while the first item's verify chain "
            "is still unsettled — this is exactly what raced two chains into "
            "the same composer in production (#133)"
        )
        q = orch._lead_notify_queue.get(TEST_PROJECT)
        assert q and len(q) == 1, "notice B must still be queued, not written"

    def test_next_item_delivered_once_chain_settles(self, orch: Orchestrator) -> None:
        """Once on_settled fires (chain concluded), the guard clears and the
        next queued item is delivered normally."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice A")
            orch._notify_lead(TEST_PROJECT, "notice B")

        assert TEST_PROJECT in orch._lead_notify_verify_active

        # Simulate the chain settling (as on_settled would after it concludes).
        orch._lead_notify_verify_active.discard(TEST_PROJECT)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        written = "".join(
            c.args[0] if isinstance(c.args[0], str) else c.args[0].decode("utf-8", "replace")
            for c in lead.session.write.call_args_list
        )
        assert "notice B" in written
        assert not orch._lead_notify_queue.get(TEST_PROJECT)

    def test_force_deliver_noop_while_pump_chain_active(self, orch: Orchestrator) -> None:
        """_force_deliver_done_notices must not write into the same Lead
        session while _pump_lead_notify's own chain is still verifying."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        orch._pending_done_notices[TEST_PROJECT] = [
            {"role": "system", "note": "notify_spill", "body": "spilled"}
        ]
        orch._lead_notify_verify_active.add(TEST_PROJECT)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._force_deliver_done_notices(TEST_PROJECT)

        lead.session.write.assert_not_called()
        # Item must remain durable — this was a no-op, not a drop.
        assert orch._pending_done_notices.get(TEST_PROJECT)
