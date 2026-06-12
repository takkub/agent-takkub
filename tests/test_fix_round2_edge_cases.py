"""Edge-case tests for fix-round-2 items not covered by test_regression_findings_2026_06.py.

Gaps addressed:
  _delayed_enter   — lambda captures session; skips write when pane.session changes
  AgentPane._on_exit gen guard — stale gen argument is silently dropped
  Orchestrator.close() — token revoked on explicit close (not only on crash/exit)
  Connection cap   — _on_new_connection rejects the (N+1)th connection when
                     _open_connections is already at _MAX_CONNECTIONS
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import _MAX_CONNECTIONS, CliServer

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def srv(qapp: QCoreApplication) -> CliServer:
    mock_orch = MagicMock()
    mock_orch._lead_token = "lead-tok"
    mock_orch._pane_tokens = {"tok-backend": ("proj", "backend")}
    mock_orch.close.return_value = (True, "closed")
    mock_orch.list_status.return_value = {}
    return CliServer(mock_orch)


# ─────────────────────────────────────────────────────────────────────────────
# _delayed_enter — session identity guard
# ─────────────────────────────────────────────────────────────────────────────


class TestDelayedEnterSessionGuard:
    """_delayed_enter() must write only to the session that was current at
    call time. If the pane is closed and respawned before the timer fires,
    the CR must be dropped, not sent to the new session."""

    def _capture_lambda(self, pane, session):
        """Call _delayed_enter with a patched QTimer.singleShot to capture the
        produced lambda without scheduling real Qt timers."""
        from agent_takkub.orchestrator import _delayed_enter

        captured: list = []

        def _fake_single_shot(delay_ms, fn):
            captured.append(fn)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", _fake_single_shot):
            _delayed_enter(pane, session, 150)

        assert len(captured) == 1, "_delayed_enter must schedule exactly one QTimer callback"
        return captured[0]

    def test_write_fires_when_session_unchanged(self) -> None:
        """CR is written when pane.session is still the captured session."""
        sess = MagicMock()
        pane = MagicMock()
        pane.session = sess

        fn = self._capture_lambda(pane, sess)
        fn()

        sess.write.assert_called_once_with(b"\r")

    def test_write_skipped_when_session_replaced(self) -> None:
        """CR is NOT written when pane.session has been replaced."""
        sess_a = MagicMock()
        sess_b = MagicMock()
        pane = MagicMock()
        pane.session = sess_a

        fn = self._capture_lambda(pane, sess_a)

        # Simulate: pane was closed and respawned with a new session before timer fires
        pane.session = sess_b

        fn()

        sess_a.write.assert_not_called()
        sess_b.write.assert_not_called()

    def test_write_skipped_when_pane_session_is_none(self) -> None:
        """CR is NOT written when pane.session is None (pane closed)."""
        sess_a = MagicMock()
        pane = MagicMock()
        pane.session = sess_a

        fn = self._capture_lambda(pane, sess_a)

        pane.session = None

        fn()

        sess_a.write.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AgentPane._on_exit generation guard
# ─────────────────────────────────────────────────────────────────────────────


class TestAgentPaneOnExitGenerationGuard:
    """AgentPane._on_exit(code, gen) must drop signals from stale sessions by
    checking the captured gen against the current _session_generation.

    Uses AgentPane.__new__ to avoid requiring a QApplication / QFrame parent.
    Only _session_generation, set_state, detach_session, state, and
    _expected_exit are needed for these logic tests.
    """

    def _make_pane(self, generation: int = 1):
        from agent_takkub.agent_pane import AgentPane

        pane = AgentPane.__new__(AgentPane)
        pane._session_generation = generation
        pane._expected_exit = False
        pane.state = "active"
        pane.set_state = MagicMock()
        pane.detach_session = MagicMock()
        return pane

    def test_current_gen_fires_set_state(self) -> None:
        """Exit with the current gen → state transition and detach happen."""
        pane = self._make_pane(generation=3)
        pane._on_exit(1, gen=3)
        pane.set_state.assert_called_once()
        pane.detach_session.assert_called_once()

    def test_stale_gen_drops_signal(self) -> None:
        """Exit with old gen → no state mutation at all."""
        pane = self._make_pane(generation=3)
        pane._on_exit(1, gen=2)  # gen 2 is stale; current is 3
        pane.set_state.assert_not_called()
        pane.detach_session.assert_not_called()

    def test_none_gen_legacy_compat_fires(self) -> None:
        """gen=None (legacy call without gen) must still fire (backwards compat)."""
        pane = self._make_pane(generation=3)
        pane._on_exit(1, gen=None)
        pane.set_state.assert_called_once()

    def test_unexpected_exit_sets_exited_state(self) -> None:
        """When _expected_exit=False, state must become 'exited'."""
        pane = self._make_pane(generation=1)
        pane._on_exit(137, gen=1)
        call_args = pane.set_state.call_args
        assert call_args[0][0] == "exited"

    def test_expected_exit_sets_empty_state(self) -> None:
        """When _expected_exit=True, state must become 'empty' (clean shutdown)."""
        pane = self._make_pane(generation=1)
        pane._expected_exit = True
        pane._on_exit(0, gen=1)
        call_args = pane.set_state.call_args
        assert call_args[0][0] == "empty"


# ─────────────────────────────────────────────────────────────────────────────
# Token revocation via Orchestrator.close()
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenRevocationOnClose:
    """Orchestrator.close() must revoke the pane's capability token so that
    a stale done/send from the closing pane is rejected immediately, not only
    after a session crash (_on_session_exit path)."""

    def _make_orch_with_token(
        self,
        project: str = "proj",
        role: str = "backend",
        token: str = "tok-close",
    ):
        orch = MagicMock()
        orch._pane_tokens = {token: (project, role)}
        orch._pane_state = {}
        orch._idle_state = {}
        orch._resolve_project = MagicMock(return_value=project)

        pane = MagicMock()
        pane.session = MagicMock()
        pane.state = "active"
        orch._project_panes = MagicMock(return_value={role: pane})
        orch._panes_by_project = {project: {role: pane}}
        return orch, token

    def test_token_revoked_after_explicit_close(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        orch, tok = self._make_orch_with_token()
        Orchestrator.close(orch, "backend", project="proj")
        assert tok not in orch._pane_tokens, (
            "capability token must be revoked when pane is explicitly closed"
        )

    def test_only_closed_role_token_revoked(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock()
        orch._pane_tokens = {
            "tok-backend": ("proj", "backend"),
            "tok-frontend": ("proj", "frontend"),
        }
        orch._pane_state = {}
        orch._idle_state = {}
        orch._resolve_project = MagicMock(return_value="proj")

        pane = MagicMock()
        pane.session = MagicMock()
        pane.state = "active"
        orch._project_panes = MagicMock(return_value={"backend": pane})
        orch._panes_by_project = {"proj": {"backend": pane}}

        Orchestrator.close(orch, "backend", project="proj")

        assert "tok-backend" not in orch._pane_tokens
        assert "tok-frontend" in orch._pane_tokens, "frontend token must survive a backend close"


# ─────────────────────────────────────────────────────────────────────────────
# Connection cap — _on_new_connection rejects N+1st connection
# ─────────────────────────────────────────────────────────────────────────────


class TestConnectionCap:
    """CliServer must not track more than _MAX_CONNECTIONS open sockets.

    When the cap is reached, the (N+1)th connection must be disconnected
    immediately and must NOT appear in _open_connections."""

    def _make_fake_qsock(self):
        sock = MagicMock()
        sock.disconnectFromHost = MagicMock()
        sock.deleteLater = MagicMock()
        # Stub slot-connection methods so srv won't throw on .connect()
        sock.readyRead = MagicMock()
        sock.readyRead.connect = MagicMock()
        sock.disconnected = MagicMock()
        sock.disconnected.connect = MagicMock()
        return sock

    def test_connection_accepted_below_cap(self, srv: CliServer, qapp: QCoreApplication) -> None:
        """A connection below the cap is tracked in _open_connections."""
        # Clear existing connections first
        srv._open_connections.clear()

        new_sock = self._make_fake_qsock()
        srv._server = MagicMock()
        srv._server.hasPendingConnections.side_effect = [True, False]
        srv._server.nextPendingConnection.return_value = new_sock

        srv._on_new_connection()

        assert new_sock in srv._open_connections
        new_sock.disconnectFromHost.assert_not_called()

    def test_connection_rejected_at_cap(self, srv: CliServer, qapp: QCoreApplication) -> None:
        """The (N+1)th connection is immediately disconnected when cap is full."""
        # Fill the table to exactly _MAX_CONNECTIONS
        srv._open_connections.clear()
        for _i in range(_MAX_CONNECTIONS):
            fake = MagicMock()
            srv._open_connections[fake] = time.time()

        new_sock = self._make_fake_qsock()
        srv._server = MagicMock()
        srv._server.hasPendingConnections.side_effect = [True, False]
        srv._server.nextPendingConnection.return_value = new_sock

        srv._on_new_connection()

        new_sock.disconnectFromHost.assert_called_once()
        assert new_sock not in srv._open_connections, "rejected connection must not be tracked"
        assert len(srv._open_connections) == _MAX_CONNECTIONS, (
            "cap size must not grow beyond _MAX_CONNECTIONS"
        )
