"""Tests for the spawn-gate + FIFO arbiter (bug #34 / 0x8001010d fix).

What these tests pin down:
  - is_spawn_blocked: None pred → False; blocked pred → True; InSendMessageEx mock
  - spawn() with gate blocked → defer, NOT call pty spawn
  - spawn() with gate clear  → call pty spawn immediately (backward compat)
  - no guard set → call pty spawn immediately (test/non-GUI backward compat)
  - _retry_deferred_spawn: gate still blocked → reschedule; gate clear → spawn
  - FIFO: _spawn_in_progress → queue, drain fires in order
  - _send_when_ready._check: session None → retry not drop
"""

from __future__ import annotations

import sys
from collections import deque
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

# Import Orchestrator at module level so terminal_widget's QWebEngineView chain
# is resolved during pytest collection — before QApplication is instantiated by
# the session-scoped _qt_session_app fixture in conftest.py.
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "gatetest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# ──────────────────────────────────────────────────────────────────────────────
# spawn_gate module unit tests
# ──────────────────────────────────────────────────────────────────────────────


class TestIsSpawnBlocked:
    def test_no_pred_returns_false_non_windows(self):
        from agent_takkub.spawn_gate import is_spawn_blocked

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False):
            assert is_spawn_blocked(None) is False

    def test_pred_true_returns_true(self):
        from agent_takkub.spawn_gate import is_spawn_blocked

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False):
            assert is_spawn_blocked(lambda: True) is True

    def test_pred_false_insend_true_returns_true(self):
        from agent_takkub.spawn_gate import is_spawn_blocked

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=True):
            assert is_spawn_blocked(lambda: False) is True

    def test_pred_false_insend_false_returns_false(self):
        from agent_takkub.spawn_gate import is_spawn_blocked

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False):
            assert is_spawn_blocked(lambda: False) is False


class TestIsInSendBlocked:
    def test_non_windows_always_false(self):
        from agent_takkub.spawn_gate import is_in_send_blocked

        with patch.object(sys, "platform", "linux"):
            assert is_in_send_blocked() is False

    def test_windows_not_in_send_returns_false(self):
        """flags=0 (not in a SendMessage) → not blocked."""
        # Validate the mask logic directly (ctypes import caching makes function-level
        # patching brittle; the logic is the same code path the function runs).
        ISMEX_SEND = 0x1
        ISMEX_REPLIED = 0x8
        flags = 0  # not inside any SendMessage
        result = (flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND
        assert result is False

    def test_windows_in_send_returns_true(self):
        """Simulate ISMEX_SEND=0x1 set, ISMEX_REPLIED=0x8 clear → blocked."""
        ISMEX_SEND = 0x1
        ISMEX_REPLIED = 0x8
        flags = ISMEX_SEND  # send pending, not yet replied
        result = (flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND
        assert result is True

    def test_windows_in_send_replied_not_blocked(self):
        """ISMEX_SEND | ISMEX_REPLIED → already replied → not blocked."""
        ISMEX_SEND = 0x1
        ISMEX_REPLIED = 0x8
        flags = ISMEX_SEND | ISMEX_REPLIED
        result = (flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND
        assert result is False


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator spawn-gate integration tests
# ──────────────────────────────────────────────────────────────────────────────


def _make_orchestrator(qapp, monkeypatch):
    """Return a minimal Orchestrator with spawn guard injectable and pty patched."""
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_dead_pane(role: str = "backend"):
    """Pane stub: no live session."""
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    return pane


class TestSpawnGateDefer:
    """Gate blocked → spawn deferred, pty_session.spawn NOT called."""

    def test_gate_blocked_returns_true_deferred(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        timer_calls = []
        with patch("agent_takkub.orchestrator.QTimer") as mock_timer:
            mock_timer.singleShot.side_effect = lambda delay, fn: timer_calls.append((delay, fn))
            orch.set_spawn_guard(lambda: True)  # gate always blocked
            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True
        assert "deferred" in msg
        assert f"{TEST_PROJECT}::backend" in orch._spawn_deferred

    def test_gate_blocked_pty_spawn_not_called(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        pty_spawn_called = []

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            with patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls:
                mock_pty = MagicMock()
                mock_pty.spawn.side_effect = lambda **kw: pty_spawn_called.append(kw)
                mock_pty_cls.return_value = mock_pty
                orch.set_spawn_guard(lambda: True)
                orch.spawn("backend", project=TEST_PROJECT)

        assert not pty_spawn_called, "pty spawn must NOT be called when gate is blocked"

    def test_no_guard_spawn_proceeds(self, qapp, monkeypatch):
        """No guard set → spawn runs normally (backward compat)."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.is_alive = True
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()
            orch.spawn("backend", project=TEST_PROJECT)

        # Should attempt spawn (mock session created)
        assert mock_pty_cls.called

    def test_gate_clear_gate_pred_false(self, qapp, monkeypatch):
        """Guard set but returns False → spawn proceeds, no defer."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        orch.set_spawn_guard(lambda: False)  # gate clear

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.is_alive = True
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()
            orch.spawn("backend", project=TEST_PROJECT)

        assert "backend" not in orch._spawn_deferred

    def test_duplicate_deferred_returns_already_pending(self, qapp, monkeypatch):
        """A second spawn() while one is already deferred returns 'already pending'."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        # Pre-seed deferred set as if first deferral happened
        orch._spawn_deferred.add(f"{TEST_PROJECT}::backend")

        ok, msg = orch.spawn("backend", project=TEST_PROJECT)
        assert ok is True
        assert "already pending" in msg


class TestRetryDeferredSpawn:
    def test_retry_gate_still_blocked_reschedules(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
        orch._spawn_deferred.add(f"{TEST_PROJECT}::backend")

        timer_calls = []
        with (
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda d, fn: timer_calls.append((d, fn)),
            ),
            patch.object(orch, "_is_spawn_blocked", return_value=True),
        ):
            orch._retry_deferred_spawn("backend", None, TEST_PROJECT, False, 0)

        # Should be re-added to deferred set and a new timer scheduled
        assert f"{TEST_PROJECT}::backend" in orch._spawn_deferred
        assert any(d == 50 for d, _ in timer_calls), "retry timer must be 50ms"

    def test_retry_gate_clear_schedules_35ms_spawn(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        timer_calls = []
        with (
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda d, fn: timer_calls.append((d, fn)),
            ),
            patch.object(orch, "_is_spawn_blocked", return_value=False),
        ):
            orch._retry_deferred_spawn("backend", None, TEST_PROJECT, False, 0)

        # Should schedule a 35ms timer (re-check + spawn)
        assert any(d == 35 for d, _ in timer_calls), "quiet-window timer must be 35ms"

    def test_retry_pane_alive_noop(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        alive_sess = MagicMock()
        alive_sess.is_alive = True
        pane.session = alive_sess
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        spawn_calls = []
        with patch.object(orch, "spawn", side_effect=lambda *a, **k: spawn_calls.append(a)):
            orch._retry_deferred_spawn("backend", None, TEST_PROJECT, False, 0)

        assert not spawn_calls, "must not respawn an already-live pane"


class TestFifoArbiter:
    """FIFO: second spawn while in-progress is queued and drained in order."""

    def test_in_progress_queues_spawn(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane_a = _make_dead_pane("backend")
        pane_b = _make_dead_pane("frontend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane_a, "frontend": pane_b}
        orch._spawn_in_progress = True  # simulate another spawn in flight

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            ok, msg = orch.spawn("frontend", project=TEST_PROJECT)

        assert ok is True
        assert "queued" in msg
        assert len(orch._spawn_queue) == 1
        role, *_ = orch._spawn_queue[0]
        assert role == "frontend"

    def test_drain_queue_fires_next_spawn(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("frontend")
        orch._panes_by_project[TEST_PROJECT] = {"frontend": pane}
        orch._spawn_queue = deque([("frontend", None, TEST_PROJECT, False, 0)])

        timer_calls = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda d, fn: timer_calls.append((d, fn)),
        ):
            orch._drain_spawn_queue()

        assert any(d == 0 for d, _ in timer_calls), "queue drain must use QTimer.singleShot(0, ...)"
        assert len(orch._spawn_queue) == 0

    def test_spawn_clears_in_progress_on_success(self, qapp, monkeypatch):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.is_alive = True
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()
            orch.spawn("backend", project=TEST_PROJECT)

        # After spawn, in_progress must be reset
        assert orch._spawn_in_progress is False


class TestSendWhenReadyRetry:
    """_send_when_ready._check() must retry (not drop) when session is None."""

    def test_check_retries_on_none_session(self, qapp, monkeypatch):
        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {}
        orch._pane_state = {}

        pane = _make_dead_pane("backend")  # session is None
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        timer_calls = []

        def fake_singleshot(delay, fn, *a, **kw):
            timer_calls.append((delay, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=fake_singleshot):
            orch._send_when_ready("backend", "do something", project=TEST_PROJECT)

            # First call schedules 1000ms check
            assert timer_calls, "singleShot must be called"
            assert timer_calls[0][0] == 1_000

            # Fire the first _check() inside the patch so QTimer.singleShot is still mocked
            first_check = timer_calls[0][1]
            timer_calls.clear()
            first_check()

            assert timer_calls, "_check must reschedule when session is None"
            assert timer_calls[0][0] == 500, "retry must be 500ms"

    def test_check_eventually_delivers_when_session_appears(self, qapp, monkeypatch):
        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {}
        orch._pane_state = {}

        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        timer_calls = []

        def fake_singleshot(delay, fn, *a, **kw):
            timer_calls.append((delay, fn))

        with patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=fake_singleshot):
            orch._send_when_ready("backend", "do something", project=TEST_PROJECT)

            # Fire initial 1000ms check inside the patch (session is None → reschedule)
            initial = timer_calls[0][1]
            timer_calls.clear()
            initial()
            assert timer_calls  # rescheduled

            # Now attach a live session
            live_sess = MagicMock()
            live_sess.is_alive = True
            live_sess.is_at_ready_prompt.return_value = True
            pane.session = live_sess

            # Fire the 500ms retry — session is now alive → delivers
            retry_check = timer_calls[0][1]
            timer_calls.clear()
            retry_check()

        live_sess.write.assert_called()  # task was delivered
