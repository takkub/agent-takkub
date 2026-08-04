"""Tests for the spawn-queue escape hatch (#139) and its [spawn-stuck] notice
to Lead (#140), plus the louder spawn_native_ms slow-path logging (#139).

Before this, an assign that fell into the spawn FIFO queue (arbiter busy)
produced NO notice to Lead at all — only an outright spawn *failure* warned
(_warn_lead_spawn_failed). If whatever held `_spawn_in_progress` never
released it, the queue (and everything behind it) was stuck forever with the
Lead none the wiser. `_check_spawn_queue_stuck` rides the existing 5s idle-
watchdog tick to detect and recover from that.
"""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.spawn_engine import (
    SPAWN_NATIVE_SLOW_MS,
    SPAWN_QUEUE_STUCK_SEC,
    _log_spawn_native_ms,
)

TEST_PROJECT = "spawnqueuetest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_dead_pane(role: str = "backend"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane.deferred_spawn = False
    return pane


def _make_live_lead_pane():
    lead = MagicMock()
    lead.session = MagicMock()
    lead.session.is_alive = True
    return lead


class TestCheckSpawnQueueStuck:
    def test_noop_when_queue_empty(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        orch._spawn_in_progress = True  # would be released if (wrongly) treated as stuck
        orch._check_spawn_queue_stuck(time.time())
        assert orch._spawn_in_progress is True

    def test_noop_when_head_is_fresh(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        now = time.time()
        orch._spawn_queue = deque([("backend", None, TEST_PROJECT, False, 0, None, now)])
        orch._spawn_in_progress = True

        orch._check_spawn_queue_stuck(now + 1.0)  # well under the threshold

        assert orch._spawn_in_progress is True
        assert len(orch._spawn_queue) == 1

    def test_noop_on_legacy_tuple_without_timestamp(self, qapp, monkeypatch):
        """A 6-item (pre-#139) queue tuple has no enqueue timestamp — must be
        left alone rather than crash or misfire on a bogus age."""
        orch = _make_orchestrator(qapp, monkeypatch)
        orch._spawn_queue = deque([("backend", None, TEST_PROJECT, False, 0, None)])
        orch._spawn_in_progress = True

        orch._check_spawn_queue_stuck(time.time() + 10_000)

        assert orch._spawn_in_progress is True
        assert len(orch._spawn_queue) == 1

    def test_stuck_head_releases_arbiter_and_drains(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
        stale_ts = time.time() - (SPAWN_QUEUE_STUCK_SEC + 5)
        orch._spawn_queue = deque([("backend", None, TEST_PROJECT, False, 0, None, stale_ts)])
        orch._spawn_in_progress = True
        orch._warn_lead_spawn_stuck = MagicMock()

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._check_spawn_queue_stuck(time.time())

        assert orch._spawn_in_progress is False, "arbiter must be forced open"
        assert len(orch._spawn_queue) == 0, "stuck head must be drained"
        orch._warn_lead_spawn_stuck.assert_called_once()
        call_role, call_project, call_age = orch._warn_lead_spawn_stuck.call_args[0]
        assert call_role == "backend"
        assert call_project == TEST_PROJECT
        assert call_age >= SPAWN_QUEUE_STUCK_SEC

    def test_warn_failure_does_not_prevent_drain(self, qapp, monkeypatch):
        """A broken warn callback (e.g. Lead pane teardown mid-check) must not
        stop the queue from being released — the escape hatch is the point."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
        stale_ts = time.time() - (SPAWN_QUEUE_STUCK_SEC + 5)
        orch._spawn_queue = deque([("backend", None, TEST_PROJECT, False, 0, None, stale_ts)])
        orch._spawn_in_progress = True
        orch._warn_lead_spawn_stuck = MagicMock(side_effect=RuntimeError("boom"))

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._check_spawn_queue_stuck(time.time())  # must not raise

        assert orch._spawn_in_progress is False
        assert len(orch._spawn_queue) == 0

    def test_idle_watchdog_tick_calls_check_spawn_queue_stuck(self, qapp, monkeypatch):
        """Wiring check: the 5s idle-watchdog tick must invoke the escape
        hatch even when nothing else triggers a new spawn() call."""
        orch = _make_orchestrator(qapp, monkeypatch)
        orch._check_spawn_queue_stuck = MagicMock()

        orch._check_idle_teammates()

        orch._check_spawn_queue_stuck.assert_called_once()
        (now_arg,) = orch._check_spawn_queue_stuck.call_args[0]
        assert isinstance(now_arg, float)


class TestSpawnEnqueuesTimestamp:
    def test_fifo_queue_entry_carries_enqueue_timestamp(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane_a = _make_dead_pane("backend")
        pane_b = _make_dead_pane("frontend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane_a, "frontend": pane_b}
        orch._spawn_in_progress = True

        t0 = time.time()
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            ok, msg = orch.spawn("frontend", project=TEST_PROJECT)

        assert ok is True
        assert "queued" in msg
        entry = orch._spawn_queue[0]
        assert len(entry) == 7
        assert entry[0] == "frontend"
        assert t0 - 1.0 <= entry[6] <= time.time() + 1.0


class TestWarnLeadSpawnStuck:
    def test_sends_spawn_stuck_notice(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        lead = _make_live_lead_pane()
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        orch._warn_lead_spawn_stuck("backend", TEST_PROJECT, 137.4)

        assert lead.session.write.called or lead.session.write.call_count >= 0
        # Message routes through _notify_lead's live-write pump; assert the
        # queued/delivered body carries the marker rather than depending on
        # exact write-call plumbing.
        written = "".join(
            c.args[0]
            for c in lead.session.write.call_args_list
            if c.args and isinstance(c.args[0], str)
        )
        assert "[spawn-stuck]" in written
        assert "backend" in written

    def test_noop_for_lead_role(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        lead = _make_live_lead_pane()
        orch._panes_by_project[TEST_PROJECT] = {"lead": lead}

        orch._warn_lead_spawn_stuck("lead", TEST_PROJECT, 200.0)

        lead.session.write.assert_not_called()

    def test_noop_when_lead_absent(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        orch._panes_by_project[TEST_PROJECT] = {}

        # Must not raise even with no Lead pane registered.
        orch._warn_lead_spawn_stuck("backend", TEST_PROJECT, 200.0)


class TestSpawnStuckIsBlockingNotice:
    def test_marker_registered_as_blocking(self):
        from agent_takkub.lead_inbox import _BLOCKING_NOTICE_MARKERS, _is_blocking_lead_notice

        assert "[spawn-stuck]" in _BLOCKING_NOTICE_MARKERS
        assert _is_blocking_lead_notice("⚠️ [spawn-stuck] backend assign ค้างใน spawn queue")


class TestLogSpawnNativeMs:
    def test_always_logs_routine_event(self):
        calls = []
        _log_spawn_native_ms(
            lambda *a, **kw: calls.append((a, kw)), role="backend", project="p", ms=10
        )
        assert calls == [(("spawn_native_ms",), {"role": "backend", "project": "p", "ms": 10})]

    def test_logs_loud_event_when_over_threshold(self):
        calls = []
        _log_spawn_native_ms(
            lambda *a, **kw: calls.append((a, kw)),
            role="backend",
            project="p",
            ms=SPAWN_NATIVE_SLOW_MS + 1,
        )
        assert len(calls) == 2
        assert calls[1][0] == ("spawn_native_slow",)
        assert calls[1][1]["ms"] == SPAWN_NATIVE_SLOW_MS + 1

    def test_no_loud_event_under_threshold(self):
        calls = []
        _log_spawn_native_ms(
            lambda *a, **kw: calls.append((a, kw)), role="backend", project="p", ms=1
        )
        assert len(calls) == 1

    def test_exactly_at_threshold_logs_loud_event(self):
        calls = []
        _log_spawn_native_ms(
            lambda *a, **kw: calls.append((a, kw)),
            role="backend",
            project="p",
            ms=SPAWN_NATIVE_SLOW_MS,
        )
        assert len(calls) == 2
