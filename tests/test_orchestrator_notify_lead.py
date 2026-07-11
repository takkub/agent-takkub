"""Tests for _notify_lead / _pump_lead_notify — ready-prompt-aware serialised delivery.

Scenarios:
  1. 2 notices arrive while Lead is busy → no write during busy, once ready both
     delivered in order, each followed by \\r via QTimer.
  2. Notice >=200 chars → payload is bracketed-paste wrapped, enter delay > 150ms.
  3. Lead dies between first and second item → second item falls into
     _pending_done_notices (durable fallback).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import (
    BRACKETED_PASTE_THRESHOLD,
    LEAD_NOTIFY_BUSY_CAP,
    Orchestrator,
    _enter_delay_ms,
    _paste_payload,
)

TEST_PROJECT = "notifytest"

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_lead_session(*, ready: bool = True) -> MagicMock:
    """Return a session mock whose is_at_ready_prompt() returns *ready*."""
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
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------


def _written_str(session: MagicMock) -> str:
    """Collect all payload strings written to a session mock."""
    parts: list[str] = []
    for c in session.write.call_args_list:
        arg = c.args[0] if c.args else ""
        if isinstance(arg, bytes):
            parts.append(arg.decode("utf-8", errors="replace"))
        else:
            parts.append(str(arg))
    return "".join(parts)


def _drain_timers(orch: Orchestrator, project_ns: str = TEST_PROJECT) -> None:
    """Run the pump for *project_ns* until the queue is empty.

    In tests QTimer.singleShot callbacks don't auto-fire, so we call
    _pump_lead_notify directly after each delivery to simulate the timer.
    Stops when the queue is empty or lead is no longer ready.
    """
    for _ in range(100):
        q = getattr(orch, "_lead_notify_queue", {}).get(project_ns)
        if not q:
            break
        orch._pump_lead_notify(project_ns)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestNotifyLeadBusyQueue:
    """Lead busy when first notice arrives → second notice queued, both delivered
    in order once Lead becomes ready."""

    def test_no_write_while_lead_is_busy(self, orch: Orchestrator) -> None:
        """If Lead is not at the ready prompt, _notify_lead must not write anything."""
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice A")

        # Lead busy → pump should not have written anything yet
        lead.session.write.assert_not_called()

    def test_two_notices_busy_then_ready_delivered_in_order(self, orch: Orchestrator) -> None:
        """Queue 2 notices while busy; flip to ready; pump delivers both in order."""
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice A")
            orch._notify_lead(TEST_PROJECT, "notice B")

        # Still busy — nothing written yet
        lead.session.write.assert_not_called()

        # Lead becomes ready — pump guard still set from busy attempt, clear it
        # then drive the pump synchronously.
        lead.session.is_at_ready_prompt.return_value = True
        pumping: set = getattr(orch, "_lead_notify_pumping", set())
        pumping.discard(TEST_PROJECT)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            _drain_timers(orch)

        # Both notices should have been written, A before B
        written = _written_str(lead.session)
        assert "notice A" in written
        assert "notice B" in written
        pos_a = written.index("notice A")
        pos_b = written.index("notice B")
        assert pos_a < pos_b, "notice A must be delivered before notice B"

    def test_each_notice_gets_enter_after(self, orch: Orchestrator) -> None:
        """Every delivered notice must be followed by \\r via QTimer.singleShot."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        timers: list[tuple[int, object]] = []

        def capture_timer(ms, fn):
            timers.append((ms, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=capture_timer):
            orch._notify_lead(TEST_PROJECT, "hello")

        # Payload write happened synchronously
        assert lead.session.write.called

        # At least one timer was scheduled (for the \\r)
        assert timers, "QTimer.singleShot must be called for the \\r submit"
        # Fire all timers to deliver \\r
        for _ms, fn in timers:
            fn()

        # Now check that \\r was written
        all_written = lead.session.write.call_args_list
        enter_calls = [c for c in all_written if c.args and c.args[0] == b"\r"]
        assert enter_calls, "\\r must be written after the payload"


class TestBracketedPasteWrapping:
    """Notices >= BRACKETED_PASTE_THRESHOLD must be wrapped + use long delay."""

    def test_long_notice_uses_bracketed_paste(self, orch: Orchestrator) -> None:
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        long_body = "X" * BRACKETED_PASTE_THRESHOLD  # at threshold → wrapped
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, long_body)

        assert lead.session.write.called
        written_arg = lead.session.write.call_args[0][0]
        # Payload must start with the bracketed-paste escape
        assert written_arg.startswith("\x1b[200~"), (
            "long notice payload must be wrapped in bracketed-paste"
        )

    def test_long_notice_uses_paste_enter_delay(self, orch: Orchestrator) -> None:
        """For a long notice the QTimer delay must exceed the typing delay (150ms)."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        long_body = "Y" * BRACKETED_PASTE_THRESHOLD
        expected_payload = _paste_payload(long_body)
        expected_delay = _enter_delay_ms(expected_payload)
        assert expected_delay > 150, "test setup: bracketed delay must exceed 150ms"

        captured_delays: list[int] = []

        def capture(ms, _fn):
            captured_delays.append(ms)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=capture):
            orch._notify_lead(TEST_PROJECT, long_body)

        # The first timer call should be for the \\r with the paste delay
        assert captured_delays, "QTimer.singleShot must be called"
        assert captured_delays[0] == expected_delay, (
            f"enter delay must be {expected_delay}ms for long notice, got {captured_delays[0]}"
        )

    def test_short_notice_not_wrapped(self, orch: Orchestrator) -> None:
        """Short notices (< threshold) must NOT be wrapped in bracketed-paste."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        short_body = "short"
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, short_body)

        assert lead.session.write.called
        written_arg = lead.session.write.call_args[0][0]
        assert not written_arg.startswith("\x1b[200~"), (
            "short notice must not be wrapped in bracketed-paste"
        )


class TestLeadDiesMidQueue:
    """Lead dies while items are still in the queue → remaining items fall into
    _pending_done_notices for durable delivery on next Lead spawn."""

    def test_remaining_items_fall_to_durable_queue(self, orch: Orchestrator) -> None:
        """Queue 2 notices while Lead is busy; then Lead dies before pump runs →
        both items must fall into _pending_done_notices."""
        # Start with Lead busy so the pump retries via QTimer (doesn't deliver)
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        # Queue both items — pump fires synchronously but Lead is busy, so it
        # schedules a QTimer retry without delivering.  We patch QTimer to no-op.
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "first")
            orch._notify_lead(TEST_PROJECT, "second")

        # Both items are now in the in-memory queue, none delivered yet
        q = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert q and len(q) == 2, f"expected 2 queued items, got {list(q)}"

        # Lead dies before it ever becomes ready
        lead.session.is_alive = False

        # Drive pump — Lead is dead, items should fall to durable queue
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        # Queue should be cleared
        q2 = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert not q2, "in-memory queue must be empty after Lead dies"

        # Both items should be in the durable queue
        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        assert any("first" in b for b in bodies), "first notice must be in durable queue"
        assert any("second" in b for b in bodies), "second notice must be in durable queue"


class TestBusyRetryCapSpill:
    """After LEAD_NOTIFY_BUSY_CAP consecutive busy-retries the pump must spill
    remaining items to _pending_done_notices and stop retrying."""

    def test_busy_over_cap_spills_to_durable(self, orch: Orchestrator) -> None:
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "notice A")
            orch._notify_lead(TEST_PROJECT, "notice B")

        # Both items are now in the in-memory queue; pump ran once (count=1).
        q = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert q and len(q) == 2, f"expected 2 queued items, got {list(q)}"

        # Drive pump manually until the cap is exceeded.
        # Initial pump already ran (count=1); LEAD_NOTIFY_BUSY_CAP more calls
        # brings count to cap+1, triggering the spill on the last call.
        for _ in range(LEAD_NOTIFY_BUSY_CAP):
            with patch("agent_takkub.orchestrator.QTimer.singleShot"):
                orch._pump_lead_notify(TEST_PROJECT)

        # In-memory queue must be drained
        q2 = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert not q2, "queue must be empty after cap exceeded"

        # Retry counter must be cleared
        retry = getattr(orch, "_lead_notify_retry", {})
        assert TEST_PROJECT not in retry, "retry counter must be cleared after spill"

        # Both items must be in the durable queue
        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        assert any("notice A" in b for b in bodies), "notice A must be in durable queue"
        assert any("notice B" in b for b in bodies), "notice B must be in durable queue"

        # Nothing must have been written to Lead's session
        lead.session.write.assert_not_called()

    def test_retry_counter_resets_after_delivery(self, orch: Orchestrator) -> None:
        """Counter must reset when Lead becomes ready and an item is delivered."""
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "hello")

        # Simulate a few retries (well below cap)
        for _ in range(3):
            with patch("agent_takkub.orchestrator.QTimer.singleShot"):
                orch._pump_lead_notify(TEST_PROJECT)

        assert getattr(orch, "_lead_notify_retry", {}).get(TEST_PROJECT, 0) == 4

        # Lead becomes ready
        lead.session.is_at_ready_prompt.return_value = True
        pumping: set = getattr(orch, "_lead_notify_pumping", set())
        pumping.discard(TEST_PROJECT)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        # Item delivered; retry counter cleared
        assert lead.session.write.called
        retry = getattr(orch, "_lead_notify_retry", {})
        assert TEST_PROJECT not in retry, "retry counter must be cleared after delivery"


class TestPumpWriteFailureDoesNotLoseItems:
    """HIGH#1 (docs/reviews/2026-07-11-full-system-review-codex.md): a
    session.write() exception (session torn down between the liveness check
    and the write) must never drop a notice — it must land in the durable
    queue instead of vanishing from both live and durable state."""

    def test_write_fails_on_only_item_spills_to_durable(self, orch: Orchestrator) -> None:
        """Single queued item; write() raises → item must survive in durable
        queue, not vanish from both live_queue and durable_queue."""
        lead = _make_lead_pane(ready=True)
        lead.session.write.side_effect = RuntimeError("session torn down")
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "only item")

        # Live queue must be empty (spilled), not silently holding a stale item.
        live = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert not live, "live queue must be cleared after a failed write"

        # The item must have survived into the durable queue.
        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        assert any("only item" in b for b in bodies), (
            "item must survive in the durable queue after a write() exception"
        )

    def test_write_fails_on_item_n_preserves_all_queued_items(self, orch: Orchestrator) -> None:
        """3 items queued while busy; when ready, write() raises on the very
        first delivery attempt (item N in a multi-item durable replay) — ALL
        3 items (not just the one being written) must survive durably."""
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._notify_lead(TEST_PROJECT, "item one")
            orch._notify_lead(TEST_PROJECT, "item two")
            orch._notify_lead(TEST_PROJECT, "item three")

        live_before = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert live_before and len(live_before) == 3

        # Lead becomes ready but its write() is now broken (torn down).
        lead.session.is_at_ready_prompt.return_value = True
        lead.session.write.side_effect = RuntimeError("session torn down")
        pumping: set = getattr(orch, "_lead_notify_pumping", set())
        pumping.discard(TEST_PROJECT)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._pump_lead_notify(TEST_PROJECT)

        live_after = getattr(orch, "_lead_notify_queue", {}).get(TEST_PROJECT)
        assert not live_after, "live queue must be cleared after a failed write"

        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        for expected in ("item one", "item two", "item three"):
            assert any(expected in b for b in bodies), (
                f"{expected!r} must survive in the durable queue — "
                "no notice may be lost when write() raises mid-pump"
            )


class TestFlushWriteFailureDoesNotLoseItems:
    """HIGH#1: _flush_pending_done_notices must not pop the whole durable
    list and persist it empty before attempting delivery — a write failure on
    the first or a middle item must leave every unacknowledged item durable."""

    def test_write_fails_on_first_replayed_item_rest_stay_durable(self, orch: Orchestrator) -> None:
        lead = _make_lead_pane(ready=True)
        lead.session.write.side_effect = RuntimeError("session torn down")
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        orch._pending_done_notices[TEST_PROJECT] = [
            {"role": "backend", "note": "notify", "body": "[backend done] first"},
            {"role": "qa", "note": "notify", "body": "[qa done] second"},
            {"role": "frontend", "note": "notify", "body": "[frontend done] third"},
        ]

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._flush_pending_done_notices(TEST_PROJECT)

        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        for expected in ("first", "second", "third"):
            assert any(expected in b for b in bodies), (
                f"{expected!r} must remain durable when the first replayed "
                "item's write fails — the old pop-all-persist-empty bug lost "
                "everything in this scenario"
            )

    def test_write_fails_on_middle_item_earlier_and_later_items_both_survive(
        self, orch: Orchestrator
    ) -> None:
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        calls = {"n": 0}

        def _write(payload):
            calls["n"] += 1
            if calls["n"] >= 2:
                # Session tears down starting with the 2nd write and stays
                # down (realistic: a torn-down session doesn't recover
                # mid-flush) — so the first item still delivers cleanly while
                # everything from the failure point on must stay durable.
                raise RuntimeError("session torn down")

        lead.session.write.side_effect = _write

        orch._pending_done_notices[TEST_PROJECT] = [
            {"role": "backend", "note": "notify", "body": "[backend done] alpha"},
            {"role": "qa", "note": "notify", "body": "[qa done] beta"},
            {"role": "frontend", "note": "notify", "body": "[frontend done] gamma"},
        ]

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._flush_pending_done_notices(TEST_PROJECT)

        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        # alpha (item 1) delivered successfully — no longer durable.
        assert not any("alpha" in b for b in bodies)
        # beta (item 2) failed to write — must still be durable.
        assert any("beta" in b for b in bodies), "middle item must survive its own write failure"
        # gamma (item 3) never reached this flush call — must still be durable.
        assert any("gamma" in b for b in bodies), (
            "items behind a failed item must remain durable, not be lost"
        )


class TestForceDeliverWriteFailureDoesNotLoseItems:
    """HIGH#1: _force_deliver_done_notices must apply the same deliver-then-ack
    rule as the live pump and the replay flush."""

    def test_write_fails_items_stay_durable(self, orch: Orchestrator) -> None:
        lead = _make_lead_pane(ready=True)
        lead.session.write.side_effect = RuntimeError("session torn down")
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        orch._pending_done_notices[TEST_PROJECT] = [
            {"role": "backend", "note": "notify_spill", "body": "[backend done] one"},
            {"role": "qa", "note": "notify_spill", "body": "[qa done] two"},
        ]

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._force_deliver_done_notices(TEST_PROJECT)

        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        bodies = [item["body"] for item in durable]
        assert any("one" in b for b in bodies)
        assert any("two" in b for b in bodies)

    def test_write_succeeds_clears_durable_queue(self, orch: Orchestrator) -> None:
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        orch._pending_done_notices[TEST_PROJECT] = [
            {"role": "backend", "note": "notify_spill", "body": "[backend done] one"},
        ]

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._force_deliver_done_notices(TEST_PROJECT)

        assert lead.session.write.called
        assert not orch._pending_done_notices.get(TEST_PROJECT)


class TestReapPendingDoneNotices:
    """Periodic reaper flushes durable done-notices when Lead returns to idle.

    Scenarios:
      1. Notice spilled to durable queue → Lead becomes idle → reaper delivers.
      2. Notice in durable queue + Lead still busy → reaper does nothing.
      3. Reaper does not double-deliver when durable queue is empty.
    """

    def _spill_to_durable(self, orch: Orchestrator, body: str) -> None:
        """Directly inject an item into _pending_done_notices (simulating a spill)."""
        orch._pending_done_notices.setdefault(TEST_PROJECT, []).append(
            {"role": "system", "note": "notify_spill", "body": body}
        )

    def test_reaper_flushes_when_lead_idle(self, orch: Orchestrator) -> None:
        """Durable notice → Lead becomes idle → _reap_pending_done_notices delivers."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        self._spill_to_durable(orch, "spilled notice")

        # Durable queue must have the notice before reaping
        assert orch._pending_done_notices.get(TEST_PROJECT), (
            "setup: durable queue should be non-empty"
        )

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._reap_pending_done_notices()

        # _flush routes through _notify_lead → synchronous write (Lead idle)
        assert lead.session.write.called, "reaper must deliver notice to idle Lead"

        # Durable queue must be cleared after flush
        assert not orch._pending_done_notices.get(TEST_PROJECT), (
            "durable queue must be empty after reaping"
        )

    def test_reaper_does_nothing_when_lead_busy(self, orch: Orchestrator) -> None:
        """Durable notice + Lead busy → reaper must not write, durable queue intact."""
        lead = _make_lead_pane(ready=False)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        self._spill_to_durable(orch, "pending notice")

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._reap_pending_done_notices()

        # Lead busy → nothing should be written
        lead.session.write.assert_not_called()

        # Durable queue must still contain the notice
        durable = orch._pending_done_notices.get(TEST_PROJECT, [])
        assert any("pending notice" in item["body"] for item in durable), (
            "durable queue must be intact when Lead is busy"
        )

    def test_reaper_noop_when_durable_empty(self, orch: Orchestrator) -> None:
        """No durable items → reaper must be a no-op (no write, no error)."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        # Ensure durable queue is empty
        orch._pending_done_notices.pop(TEST_PROJECT, None)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._reap_pending_done_notices()

        lead.session.write.assert_not_called()

    def test_reaper_no_double_deliver_with_flush_on_spawn(self, orch: Orchestrator) -> None:
        """pop() in _flush_pending_done_notices guarantees single delivery even
        if reaper and spawn-flush race (pop is atomic in CPython)."""
        lead = _make_lead_pane(ready=True)
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}
        self._spill_to_durable(orch, "once only")

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            # First flush (simulates spawn-path flush)
            orch._flush_pending_done_notices(TEST_PROJECT)
            # Second call (simulates reaper firing just after)
            orch._reap_pending_done_notices()

        # Body written exactly once (the second flush finds an empty queue)
        written = _written_str(lead.session)
        assert written.count("once only") == 1, (
            "notice must be delivered exactly once even if flush called twice"
        )
