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


class TestDelayedEnterVerified:
    """_delayed_enter_verified() must resend the submitting CR when it was
    swallowed mid-paste-render (#22) — detected by the pane STILL being at its
    ready prompt after the Enter should have submitted — and stop as soon as
    the submit lands (ready prompt drops) or the resend budget runs out."""

    def _run_inline(self, pane, session, *, max_resends):
        """Run _delayed_enter_verified with QTimer.singleShot firing inline so
        the whole delay→submit→verify→resend chain executes synchronously."""
        from agent_takkub.orchestrator import _delayed_enter_verified

        resends: list[int] = []

        def _inline(delay_ms, fn):
            fn()

        with patch("agent_takkub.orchestrator.QTimer.singleShot", _inline):
            _delayed_enter_verified(
                pane, session, 150, max_resends=max_resends, on_resend=resends.append
            )
        return resends

    def test_no_resend_when_submit_lands(self) -> None:
        """Submit landed → pane goes busy → is_at_ready_prompt False → one CR."""
        sess = MagicMock()
        sess.is_at_ready_prompt.return_value = False  # busy after submit
        pane = MagicMock()
        pane.session = sess

        resends = self._run_inline(pane, sess, max_resends=3)

        sess.write.assert_called_once_with(b"\r")
        assert resends == []

    def test_resends_until_ready_drops(self) -> None:
        """Ready stays True for two checks (swallowed) then drops → 1 initial +
        2 resend CRs, and stops once the submit lands."""
        sess = MagicMock()
        # verify #1 True (resend), verify #2 True (resend), verify #3 False (stop)
        sess.is_at_ready_prompt.side_effect = [True, True, False]
        pane = MagicMock()
        pane.session = sess

        resends = self._run_inline(pane, sess, max_resends=3)

        assert sess.write.call_count == 3  # initial + 2 resends
        assert all(c.args == (b"\r",) for c in sess.write.call_args_list)
        assert len(resends) == 2

    def test_resend_budget_is_bounded(self) -> None:
        """A pane that never leaves the ready prompt must not loop forever —
        resends are capped at max_resends."""
        sess = MagicMock()
        sess.is_at_ready_prompt.return_value = True  # never submits
        pane = MagicMock()
        pane.session = sess

        resends = self._run_inline(pane, sess, max_resends=3)

        # 1 initial + exactly 3 resends, then the budget is exhausted.
        assert sess.write.call_count == 1 + 3
        assert len(resends) == 3

    def test_stops_when_session_replaced_before_verify(self) -> None:
        """If the pane is respawned between the Enter and the verify, no resend
        targets the replacement session."""
        from agent_takkub.orchestrator import _delayed_enter_verified

        sess_a = MagicMock()
        sess_a.is_at_ready_prompt.return_value = True
        sess_b = MagicMock()
        pane = MagicMock()
        pane.session = sess_a

        # Fire the initial-enter timer (writes to sess_a), then swap the session
        # before the verify timer runs, so the verify guard drops it.
        timers: list = []

        def _queue(delay_ms, fn):
            timers.append(fn)

        with patch("agent_takkub.orchestrator.QTimer.singleShot", _queue):
            _delayed_enter_verified(pane, sess_a, 150, max_resends=3)
            timers.pop(0)()  # delay → _send_then_verify → initial CR + schedule verify
            pane.session = sess_b  # respawn before verify fires
            while timers:
                timers.pop(0)()

        sess_a.write.assert_called_once_with(b"\r")  # only the initial CR
        sess_b.write.assert_not_called()


class TestDelayedEnterVerifiedRepaste:
    """#79: when ``payload`` is supplied, a submit that didn't land is recovered
    by re-pasting (swallowed paste — input box empty, the #26 'pane stays empty'
    symptom) rather than only resending the CR (swallowed Enter — content still
    in the box, #22). The CR-only path must be unchanged when the input shows
    pending content, and untouched entirely when no payload is supplied."""

    PAYLOAD = "\x1b[200~[ROLE: qa] do the thing\x1b[201~"

    def _run_inline(self, pane, session):
        from agent_takkub.orchestrator import _delayed_enter_verified

        resends: list[int] = []
        repastes: list[int] = []

        def _inline(delay_ms, fn):
            fn()

        with patch("agent_takkub.orchestrator.QTimer.singleShot", _inline):
            _delayed_enter_verified(
                pane,
                session,
                150,
                max_resends=3,
                # Explicit (small) busy budget so the not-ready/boot-path resend
                # values are predictable and decoupled from the production
                # _SUBMIT_BUSY_MAX_RESENDS default. The not-ready branch reports
                # busy_remaining to on_resend; the ready branch reports remaining.
                busy_max_resends=50,
                payload=self.PAYLOAD,
                content_fragment="[ROLE: qa] do the thing",
                on_resend=resends.append,
                on_repaste=repastes.append,
            )
        return resends, repastes

    def test_repaste_when_input_empty(self) -> None:
        """Paste swallowed (input empty AND pane idle) → re-paste payload then
        CR, not a bare CR resend that would submit nothing into an empty box.

        A genuinely swallowed paste leaves the pane idle, so seconds_since_output
        is large (≥ _RENDER_ACTIVE_S): the render-settle guard is skipped and the
        repaste fires immediately, preserving the #26 fix."""
        sess = MagicMock()
        # verify #1: still ready (submit didn't land); verify #2 after re-paste:
        # busy (landed) → stop.
        sess.is_at_ready_prompt.side_effect = [True, False]
        sess.shows_pending_input.return_value = False  # input box empty
        sess.seconds_since_output.return_value = 5.0  # idle → not still rendering
        # Genuine swallow: the pane produced NO output since the paste, so the
        # baseline never advances → produced_output is False → repaste fires.
        sess.last_output_monotonic.return_value = 100.0
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        # initial CR, then re-paste payload, then submitting CR.
        assert writes == [b"\r", self.PAYLOAD, b"\r"]
        assert repastes == [3] and resends == []

    def test_no_repaste_when_paste_landed_then_idle(self) -> None:
        """Duplicate-task false-positive (the 'เบิ้ลตามจำนวนโปรเจค' report): a paste
        that was SUBMITTED leaves the pane idle with an empty box — pixel-identical
        to a swallowed paste. The discriminator is output: if claude produced any
        output since the paste (placeholder/reply rendered), the bytes were
        received, so an empty box means it was submitted, NOT lost. Must recover
        with a bare CR (harmless no-op) and NEVER re-paste — re-pasting here is
        what stacked duplicate task specs in the teammate's input box."""
        sess = MagicMock()
        sess.is_at_ready_prompt.side_effect = [True, False]  # idle, then stop
        sess.shows_pending_input.return_value = False  # empty box (already sent)
        sess.seconds_since_output.return_value = 5.0  # idle now
        # Output advanced AFTER the baseline → claude received the paste.
        sess.last_output_monotonic.side_effect = [100.0, 200.0]  # baseline, verify
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        assert self.PAYLOAD not in writes  # never a second [Pasted text]
        assert writes == [b"\r", b"\r"]  # initial CR + one harmless CR resend
        assert repastes == [] and resends == [3]

    def test_render_lag_waits_instead_of_repasting(self) -> None:
        """Duplicate-paste spam fix: an empty-LOOKING box while the pane is still
        producing output (placeholder mid-render under parallel-spawn load) must
        NOT repaste. The guard re-polls until the placeholder appears, then the
        already-landed paste only needs a CR — never a second [Pasted text]."""
        sess = MagicMock()
        # v1: ready + empty (rendering) → wait; v2: ready + placeholder now shows
        # → CR resend; v3: busy (landed), box cleared → stop (#99 cross-check
        # sees no pending input, so it doesn't fire a second resend here).
        sess.is_at_ready_prompt.side_effect = [True, True, False]
        sess.shows_pending_input.side_effect = [False, True, False]
        sess.seconds_since_output.return_value = 0.2  # output recent → rendering
        # No output since the paste yet (placeholder still painting), so the
        # produced_output early-exit does NOT trigger — the render-settle wait
        # owns this case and resolves it with a CR once the placeholder appears.
        sess.last_output_monotonic.return_value = 100.0
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        # initial CR, waited out the render, then one CR resend — payload pasted
        # exactly once (by the caller), never re-pasted.
        assert writes == [b"\r", b"\r"]
        assert repastes == [] and resends == [3]

    def test_render_lag_repaste_still_bounded(self) -> None:
        """Pathological: pane keeps producing output yet the box never shows the
        placeholder. The render-wait must be bounded so it still falls through to
        a repaste rather than looping forever, and the repaste budget caps the
        total so it cannot spam indefinitely."""
        sess = MagicMock()
        sess.is_at_ready_prompt.return_value = True  # never lands
        sess.shows_pending_input.return_value = False  # box never shows content
        sess.seconds_since_output.return_value = 0.1  # always "rendering"
        # Silent since the paste (baseline never advances) → produced_output stays
        # False, so each attempt waits out the bounded grace then repastes.
        sess.last_output_monotonic.return_value = 100.0
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        # Repastes are still capped at max_resends despite the render-wait guard.
        assert repastes == [3, 2, 1] and resends == []

    def test_cr_only_when_content_present(self) -> None:
        """Enter swallowed but the pasted content is still in the box (#22) →
        resend CR only, never re-paste (would duplicate the content)."""
        sess = MagicMock()
        # verify #1: ready + content → resend. verify #2: not-ready + content
        # still present (#99 cross-check) → resend again. verify #3: not-ready,
        # box finally cleared → stop.
        sess.is_at_ready_prompt.side_effect = [True, False, False]
        sess.shows_pending_input.side_effect = [True, True, False]
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        # initial CR, ready+content resend, then not-ready+content resend
        # (#99: not-ready is cross-checked against the box before trusting it).
        assert writes == [b"\r", b"\r", b"\r"]
        # First resend is the ready+content path (reports remaining=3); the
        # second is the not-ready/#99 path, which now draws on the separate busy
        # budget and reports busy_remaining (=50, the explicit test override).
        assert resends == [3, 50] and repastes == []
        sess.shows_pending_input.assert_called()

    def test_not_ready_stops_when_content_cleared(self) -> None:
        """Not-ready + input box empty (content genuinely submitted, or a
        payload-less caller) must still stop — the #99 cross-check only
        overrides the stop when the box demonstrably still holds the paste."""
        sess = MagicMock()
        sess.is_at_ready_prompt.return_value = False  # busy after submit
        sess.shows_pending_input.return_value = False  # box empty — landed
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        sess.write.assert_called_once_with(b"\r")  # only the initial CR
        assert resends == [] and repastes == []

    def test_not_ready_busy_marker_unrelated_to_submit_recovers(self) -> None:
        """Regression for #99: a busy marker unrelated to this submit (codex
        auto-booting its configured MCP servers shows 'esc to interrupt' while
        the composer still holds the unsubmitted paste underneath it) must not
        be mistaken for "submit landed, pane now genuinely busy". Sequence
        lifted from the observed transcript (codex-085447.transcript.log):
        paste delivered → pane reads not-ready during MCP boot with the paste
        placeholder still in the box → must resend, not fall silent forever →
        once MCP boot finishes and the CR lands the box finally clears."""
        sess = MagicMock()
        # verify #1: not-ready (MCP boot's "esc to interrupt") — resend.
        # verify #2: not-ready still (MCP boot ongoing) — resend again.
        # verify #3: not-ready, MCP boot done + resent CR landed — box clears → stop.
        sess.is_at_ready_prompt.return_value = False
        sess.shows_pending_input.side_effect = [True, True, False]
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        assert writes == [b"\r", b"\r", b"\r"]  # initial + 2 recovery resends
        # Both recovery resends are the not-ready/MCP-boot path, so both report
        # the busy budget countdown (busy_max_resends=50 → 50, 49) — the whole
        # point of the fix: MCP boot is nudged on the large budget, not the
        # tiny 3-resend swallow budget it used to share.
        assert resends == [50, 49] and repastes == []

    def test_transcript_sequence_busy_mcp_boot_then_ready_still_pending(self) -> None:
        """Regression for #99, exact chronology from the transcript: paste →
        NOT-ready during MCP boot with the paste still stuck (#99 cross-check
        fires, resend #1) → MCP boot finishes, pane reads READY but the
        resent CR was ALSO swallowed mid-render so the box still shows the
        paste (pre-existing #22 branch fires, resend #2) → the second resend
        finally lands and codex starts working the task (not-ready, box
        clear) → stop. Proves the new not-ready cross-check and the
        pre-existing ready+content-present recovery chain into each other
        instead of the first one giving up before the second ever runs."""
        sess = MagicMock()
        sess.is_at_ready_prompt.side_effect = [False, True, False]
        sess.shows_pending_input.side_effect = [True, True, False]
        pane = MagicMock()
        pane.session = sess

        resends, repastes = self._run_inline(pane, sess)

        writes = [c.args[0] for c in sess.write.call_args_list]
        assert writes == [b"\r", b"\r", b"\r"]  # initial + 2 recovery resends
        # resend #1 is the not-ready/MCP-boot path (busy budget → 50); resend #2
        # is the ready+content-present (#22) path (swallow budget → remaining 3).
        # The two chain into each other exactly as before — only the first now
        # draws on the larger budget so a longer boot can't starve it.
        assert resends == [50, 3] and repastes == []


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
        from agent_takkub.agent_pane_model import AgentPaneModel
        from agent_takkub.roles import by_name

        pane = AgentPane.__new__(AgentPane)
        # _session_generation/_expected_exit/state live on self.model
        # (issue #105 Phase A) — seed a bare model too.
        pane.model = AgentPaneModel(by_name("backend"))
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
