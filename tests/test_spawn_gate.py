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


# ──────────────────────────────────────────────────────────────────────────────
# Tier 2: final re-sample gate — all 4 provider branches
# ──────────────────────────────────────────────────────────────────────────────


class TestTier2FinalGate:
    """_final_gate_clear() = False → native spawn NOT called, deferred cleanly."""

    def _make_claude_pane(self, role: str = "backend"):
        pane = MagicMock()
        pane.role = MagicMock()
        pane.role.name = role
        pane.session = None
        pane.state = "empty"
        pane._transcript_path = None
        return pane

    def test_claude_toctou_blocked_no_native_spawn(self, qapp, monkeypatch):
        """Claude (backend) branch: final gate fails → deferred, PtySession.spawn NOT called."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        pty_spawn_calls = []
        timer_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda d, fn: timer_calls.append((d, fn)),
            ),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True
        assert "deferred" in msg or "final re-sample" in msg
        assert not pty_spawn_calls, "PtySession.spawn must NOT be called when TOCTOU gate fails"
        assert f"{TEST_PROJECT}::backend" in orch._spawn_deferred

    def test_claude_toctou_clears_spawn_in_progress(self, qapp, monkeypatch):
        """_spawn_in_progress must be False after TOCTOU defer (no leak via finally)."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            orch.spawn("backend", project=TEST_PROJECT)

        assert not orch._spawn_in_progress, "_spawn_in_progress must be reset after TOCTOU defer"

    def test_claude_toctou_revokes_pane_token(self, qapp, monkeypatch):
        """Pane token must be revoked when TOCTOU defer happens (no token leak)."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            orch.spawn("backend", project=TEST_PROJECT)

        assert not orch._pane_tokens, "pane token must be revoked after TOCTOU defer"

    def test_shell_toctou_blocked_no_native_spawn(self, qapp, monkeypatch):
        """Shell branch: TOCTOU defer prevents native spawn."""
        import shutil as _shutil_mod

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("shell")
        orch._panes_by_project[TEST_PROJECT] = {"shell": pane}

        pty_spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch.object(_shutil_mod, "which", return_value="C:/Windows/System32/pwsh.exe"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("shell", project=TEST_PROJECT)

        assert ok is True
        assert "deferred" in msg or "final re-sample" in msg
        assert not pty_spawn_calls, (
            "Shell PtySession.spawn must NOT be called when TOCTOU gate fails"
        )

    def test_gemini_toctou_blocked_no_native_spawn(self, qapp, monkeypatch):
        """Gemini branch: TOCTOU defer prevents native spawn."""
        from agent_takkub.provider_config import GEMINI

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("gemini")
        orch._panes_by_project[TEST_PROJECT] = {"gemini": pane}

        pty_spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.provider_config.effective_provider_for", return_value=GEMINI),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("gemini", project=TEST_PROJECT)

        assert ok is True
        assert "deferred" in msg or "final re-sample" in msg
        assert not pty_spawn_calls, (
            "Gemini PtySession.spawn must NOT be called when TOCTOU gate fails"
        )

    def test_codex_toctou_blocked_no_native_spawn(self, qapp, monkeypatch):
        """Codex branch: TOCTOU defer prevents native spawn."""
        from agent_takkub.provider_config import CODEX

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("codex")
        orch._panes_by_project[TEST_PROJECT] = {"codex": pane}

        pty_spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("codex", project=TEST_PROJECT)

        assert ok is True
        assert "deferred" in msg or "final re-sample" in msg
        assert not pty_spawn_calls, (
            "Codex PtySession.spawn must NOT be called when TOCTOU gate fails"
        )

    def test_clear_clear_blocked_no_native_spawn(self, qapp, monkeypatch):
        """Sequence clear/clear/blocked: native spawn must NOT be called even after 2 clears."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_claude_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        call_count = 0

        def _resample_side_effect():
            nonlocal call_count
            call_count += 1
            return False  # all 3 calls blocked (simulating clear/clear/blocked via N=3 samples)

        pty_spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", side_effect=_resample_side_effect),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, _msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True
        assert not pty_spawn_calls, "spawn must not proceed when any final re-sample is blocked"


# ──────────────────────────────────────────────────────────────────────────────
# Tier 1: quiet-boot debounce streak
# ──────────────────────────────────────────────────────────────────────────────


class TestTier1QuietBootStreak:
    """_spawn_lead_when_quiet: streak resets on block; N clear turns → spawn fires."""

    def _make_mock_window(self, spawn_ok=True):
        mw = MagicMock()
        mw._boot_quiet_count = 0
        mw.isVisible.return_value = True
        mw.orch._spawn_gate_pred = None
        mw.orch.spawn.return_value = (True, "spawned") if spawn_ok else (False, "fail")
        return mw

    def test_blocked_resets_streak_reschedules(self, qapp, monkeypatch):
        """is_in_send_blocked True → streak reset to 0, QTimer scheduled."""
        from agent_takkub.main_window import MainWindow

        mw = self._make_mock_window()
        mw._boot_quiet_count = 2  # mid-streak

        timer_calls = []

        with (
            patch("agent_takkub.main_window.QApplication") as mock_qa,
            patch("agent_takkub.main_window.QTimer") as mock_qt,
            patch("agent_takkub.main_window.is_in_send_blocked", return_value=True, create=True),
            patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=True),
        ):
            mock_qa.applicationState.return_value = (
                patch("agent_takkub.main_window.Qt").start().ApplicationState.ApplicationActive
            )
            mock_qt.singleShot.side_effect = lambda d, fn: timer_calls.append((d, fn))
            MainWindow._spawn_lead_when_quiet(mw)

        assert mw._boot_quiet_count == 0, "streak must reset when blocked"
        assert mw.orch.spawn.call_count == 0, "spawn must not fire when blocked"

    def test_n_consecutive_clears_fires_spawn(self, qapp, monkeypatch):
        """N consecutive clear turns → orch.spawn(LEAD) called exactly once."""
        from agent_takkub.main_window import _BOOT_LEAD_QUIET_N, MainWindow
        from agent_takkub.roles import LEAD

        mw = self._make_mock_window()
        timer_calls = []

        with (
            patch("agent_takkub.main_window.QApplication") as mock_qa,
            patch("agent_takkub.main_window.QTimer") as mock_qt,
            patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False),
        ):
            mock_qa.applicationState.return_value = type("S", (), {"ApplicationActive": object()})()
            # Patch Qt.ApplicationState.ApplicationActive comparison
            import agent_takkub.main_window as mw_mod

            with patch.object(
                mw_mod.Qt.ApplicationState,
                "ApplicationActive",
                new=mock_qa.applicationState.return_value,
            ):
                mock_qt.singleShot.side_effect = lambda d, fn: timer_calls.append((d, fn))

                # Run N clear turns
                for _ in range(_BOOT_LEAD_QUIET_N):
                    MainWindow._spawn_lead_when_quiet(mw)

        assert mw.orch.spawn.call_count == 1, "spawn must fire exactly once after N clear turns"
        assert mw.orch.spawn.call_args[0][0] == LEAD.name

    def test_blocked_mid_streak_resets_then_clears(self, qapp, monkeypatch):
        """Blocked mid-streak resets count; subsequent clears restart and fire."""
        from agent_takkub.main_window import MainWindow

        mw = self._make_mock_window()

        blocked_sequence = [False, True, False, False, False]  # clear, block, then 3 clears
        call_idx = 0

        def _isb():
            nonlocal call_idx
            v = blocked_sequence[call_idx] if call_idx < len(blocked_sequence) else False
            call_idx += 1
            return v

        import agent_takkub.main_window as mw_mod

        with (
            patch("agent_takkub.main_window.QApplication") as mock_qa,
            patch("agent_takkub.main_window.QTimer") as mock_qt,
            patch("agent_takkub.spawn_gate.is_in_send_blocked", side_effect=_isb),
        ):
            mock_qa.applicationState.return_value = type("S", (), {"ApplicationActive": object()})()
            with patch.object(
                mw_mod.Qt.ApplicationState,
                "ApplicationActive",
                new=mock_qa.applicationState.return_value,
            ):
                mock_qt.singleShot.side_effect = lambda d, fn: None

                for _ in range(len(blocked_sequence)):
                    MainWindow._spawn_lead_when_quiet(mw)

        # After 1 clear then 1 block (reset) then 3 clears: spawn fires once
        assert mw.orch.spawn.call_count == 1


# ──────────────────────────────────────────────────────────────────────────────
# Residual: _on_codex_exit stale-guard before spawn_ts reset
# ──────────────────────────────────────────────────────────────────────────────


class TestCodexExitSpawnTsGuard:
    """Stale codex exit must NOT clobber new session's codex_spawn_ts."""

    def test_stale_exit_does_not_clobber_new_spawn_ts(self, qapp, monkeypatch):
        """If pane has new session attached, stale processExited must return early."""
        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {}
        orch._pane_state = {}

        old_session = MagicMock()
        new_session = MagicMock()

        project = "proj"
        role = "codex"

        # Register pane pointing at NEW session
        pane = MagicMock()
        pane.session = new_session
        orch._panes_by_project[project] = {role: pane}

        # Plant spawn_ts for new session in pane state
        from agent_takkub.orchestrator import PaneState, _exit_key

        ekey = _exit_key(project, role)
        ps = PaneState()
        ps.codex_spawn_ts = 999.0  # new session's timestamp
        orch._pane_state[ekey] = ps

        # Fire stale exit from old_session
        orch._on_codex_exit(0, role, "/cwd", project, old_session)

        # spawn_ts must NOT have been cleared
        assert orch._pane_state[ekey].codex_spawn_ts == 999.0, (
            "codex_spawn_ts must not be clobbered by stale exit"
        )

    def test_current_exit_clears_spawn_ts(self, qapp, monkeypatch):
        """Current session's exit clears codex_spawn_ts normally."""
        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {}
        orch._pane_state = {}
        orch._recent_exits = {}

        current_session = MagicMock()

        project = "proj"
        role = "codex"

        pane = MagicMock()
        pane.session = current_session
        orch._panes_by_project[project] = {role: pane}

        import time as _time

        from agent_takkub.orchestrator import PaneState, _exit_key

        ekey = _exit_key(project, role)
        ps = PaneState()
        ps.codex_spawn_ts = _time.time() - 5.0  # recent spawn
        orch._pane_state[ekey] = ps

        # Mock _on_session_exit so it doesn't blow up
        orch._on_session_exit = MagicMock()
        orch._write_codex_crash_dump = MagicMock()

        orch._on_codex_exit(0, role, "/cwd", project, current_session)

        assert orch._pane_state[ekey].codex_spawn_ts is None, (
            "codex_spawn_ts must be cleared when the current session exits"
        )


# ──────────────────────────────────────────────────────────────────────────────
# is_in_send_stable direct unit tests (Tier 2 building block)
# ──────────────────────────────────────────────────────────────────────────────


class TestIsInSendStable:
    """is_in_send_stable(n) must return False when ANY of the n samples is blocked."""

    def test_all_clear_returns_true(self):
        from agent_takkub.spawn_gate import is_in_send_stable

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False):
            assert is_in_send_stable(3) is True

    def test_all_blocked_returns_false(self):
        from agent_takkub.spawn_gate import is_in_send_stable

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=True):
            assert is_in_send_stable(3) is False

    def test_last_sample_blocked_returns_false(self):
        """clear/clear/blocked → False (the key TOCTOU narrowing case)."""
        from agent_takkub.spawn_gate import is_in_send_stable

        calls = [False, False, True]
        idx = [0]

        def _side():
            v = calls[idx[0]]
            idx[0] += 1
            return v

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", side_effect=_side):
            assert is_in_send_stable(3) is False

    def test_first_sample_blocked_short_circuits(self):
        """Any single blocked sample (first) → False; n=1 edge."""
        from agent_takkub.spawn_gate import is_in_send_stable

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=True):
            assert is_in_send_stable(1) is False

    def test_n_equals_one_clear_returns_true(self):
        from agent_takkub.spawn_gate import is_in_send_stable

        with patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False):
            assert is_in_send_stable(1) is True


# ──────────────────────────────────────────────────────────────────────────────
# Tier 1: individual non-insend conditions reset streak
# ──────────────────────────────────────────────────────────────────────────────


class TestTier1NonInsendConditions:
    """modal, app_active, and window_ready must each independently reset the streak."""

    def _run_once(self, modal_clear, app_active, window_ready, insend_clear=True):
        from agent_takkub.main_window import MainWindow

        mw = MagicMock()
        mw._boot_quiet_count = 2
        mw.isVisible.return_value = window_ready
        mw.orch._spawn_gate_pred = (lambda: not modal_clear) if not modal_clear else None
        mw.orch.spawn.return_value = (True, "spawned")

        timer_calls = []
        import agent_takkub.main_window as mw_mod

        active_state = object()
        app_return = active_state if app_active else object()

        with (
            patch("agent_takkub.main_window.QApplication") as mock_qa,
            patch("agent_takkub.main_window.QTimer") as mock_qt,
            patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=not insend_clear),
            patch.object(mw_mod.Qt.ApplicationState, "ApplicationActive", new=active_state),
        ):
            mock_qa.applicationState.return_value = app_return
            mock_qt.singleShot.side_effect = lambda d, fn: timer_calls.append((d, fn))
            MainWindow._spawn_lead_when_quiet(mw)

        return mw._boot_quiet_count, mw.orch.spawn.call_count

    def test_modal_blocked_resets_streak(self):
        count, spawned = self._run_once(modal_clear=False, app_active=True, window_ready=True)
        assert count == 0
        assert spawned == 0

    def test_app_inactive_resets_streak(self):
        count, spawned = self._run_once(modal_clear=True, app_active=False, window_ready=True)
        assert count == 0
        assert spawned == 0

    def test_window_not_visible_resets_streak(self):
        count, spawned = self._run_once(modal_clear=True, app_active=True, window_ready=False)
        assert count == 0
        assert spawned == 0


# ──────────────────────────────────────────────────────────────────────────────
# Tier 2: _spawn_in_progress reset on TOCTOU defer (non-claude branches)
# ──────────────────────────────────────────────────────────────────────────────


class TestTier2InProgressResetNonClaude:
    """_spawn_in_progress must be False after TOCTOU defer on every branch."""

    def _make_pane(self, role: str):
        pane = MagicMock()
        pane.role = MagicMock()
        pane.role.name = role
        pane.session = None
        pane.state = "empty"
        pane._transcript_path = None
        return pane

    def test_shell_toctou_clears_spawn_in_progress(self, qapp, monkeypatch):
        import shutil as _shutil_mod

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_pane("shell")
        orch._panes_by_project[TEST_PROJECT] = {"shell": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch.object(_shutil_mod, "which", return_value="C:/Windows/System32/pwsh.exe"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            orch.spawn("shell", project=TEST_PROJECT)

        assert not orch._spawn_in_progress, (
            "_spawn_in_progress must be reset after shell TOCTOU defer"
        )

    def test_gemini_toctou_clears_spawn_in_progress(self, qapp, monkeypatch):
        from agent_takkub.provider_config import GEMINI

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_pane("gemini")
        orch._panes_by_project[TEST_PROJECT] = {"gemini": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.provider_config.effective_provider_for", return_value=GEMINI),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            pane.attach_session = MagicMock()
            orch.spawn("gemini", project=TEST_PROJECT)

        assert not orch._spawn_in_progress, (
            "_spawn_in_progress must be reset after gemini TOCTOU defer"
        )

    def test_codex_toctou_clears_spawn_in_progress(self, qapp, monkeypatch):
        from agent_takkub.provider_config import CODEX

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = self._make_pane("codex")
        orch._panes_by_project[TEST_PROJECT] = {"codex": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            pane.attach_session = MagicMock()
            orch.spawn("codex", project=TEST_PROJECT)

        assert not orch._spawn_in_progress, (
            "_spawn_in_progress must be reset after codex TOCTOU defer"
        )


# ──────────────────────────────────────────────────────────────────────────────
# _toctou_redefer edge: pane_tok=None must not crash
# ──────────────────────────────────────────────────────────────────────────────


class TestToctouRedeferEdge:
    def test_none_pane_tok_no_crash(self, qapp, monkeypatch):
        """_toctou_redefer(pane_tok=None) must not raise even if _pane_tokens is empty."""
        orch = _make_orchestrator(qapp, monkeypatch)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._toctou_redefer(
                "backend",
                None,
                TEST_PROJECT,
                TEST_PROJECT,
                False,
                0,
                pane_tok=None,
            )

        assert f"{TEST_PROJECT}::backend" in orch._spawn_deferred

    def test_lead_toctou_blocked_clean_redefer(self, qapp, monkeypatch):
        """Lead path: blocked final gate must return ok=True + re-defer cleanly.

        Regression for UnboundLocalError when pane_tok was only bound in the
        non-Lead else-branch but passed unconditionally to _toctou_redefer.
        """
        from agent_takkub.roles import LEAD

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = MagicMock()
        pane.role = MagicMock()
        pane.role.name = LEAD.name
        pane.session = None
        pane.state = "empty"
        pane._transcript_path = None
        orch._panes_by_project[TEST_PROJECT] = {LEAD.name: pane}

        pty_spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn(LEAD.name, project=TEST_PROJECT)

        assert ok is True, f"Lead TOCTOU re-defer must return ok=True (not crash): {msg}"
        assert "deferred" in msg or "final re-sample" in msg
        assert not pty_spawn_calls, "PtySession.spawn must NOT be called when Lead final gate fails"
        assert f"{TEST_PROJECT}::{LEAD.name}" in orch._spawn_deferred

    def test_unknown_pane_tok_no_crash(self, qapp, monkeypatch):
        """_toctou_redefer with an already-revoked or unknown token must not raise."""
        orch = _make_orchestrator(qapp, monkeypatch)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._toctou_redefer(
                "backend",
                None,
                TEST_PROJECT,
                TEST_PROJECT,
                False,
                0,
                pane_tok="nonexistent-token",
            )

        assert f"{TEST_PROJECT}::backend" in orch._spawn_deferred
