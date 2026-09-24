"""Regression tests for #721 — busy-queue submit delivery (codex).

While a codex turn is running, Enter does NOT submit the composer: it just
leaves the pasted task as an unsubmitted draft and the ready-footer takes
over when the turn ends, so the old verify chain mis-reported "delivered".
Codex instead shows the footer hint "tab to queue message"; pressing Tab
queues the draft and "Queued follow-up inputs" / "Messages to be submitted"
appears above the input line.

The fix:
  * `_delayed_enter_verified` writes the provider's busy-queue key (Tab), not
    Enter, when `shows_busy_queue_marker` is on screen, and verifies arrival
    with the queue-confirm markers instead of the ready prompt.
  * The settle outcome (`SubmitSettleOutcome`) tells the caller whether the
    submit used the queue key and whether the paste is STILL stuck as an
    unsubmitted draft — the stuck case must never be counted as delivered.
  * All three strings live in `provider_spec.ProviderSpec`, not hardcoded.

The other providers surveyed for the same incident (claude/agy/opencode) all
submit with Enter while busy (claude queues, opencode/agy interrupt), so they
keep the legacy Enter path — asserted in the provider-spec wiring test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_takkub.lead_inbox import SubmitSettleOutcome, _delayed_enter_verified


def _run_inline(pane, session, **kwargs):
    """Run _delayed_enter_verified with QTimer.singleShot firing inline so the
    whole delay→submit→verify chain executes synchronously (same technique as
    TestDelayedEnterVerified in test_fix_round2_edge_cases.py)."""
    outcomes: list[SubmitSettleOutcome] = []
    resends: list[int] = []
    queued: list[bool] = []

    def _inline(delay_ms, fn):
        fn()

    with patch("agent_takkub.lead_inbox.QTimer.singleShot", _inline):
        _delayed_enter_verified(
            pane,
            session,
            150,
            max_resends=3,
            on_settled=outcomes.append,
            on_resend=resends.append,
            on_queued=lambda: queued.append(True),
            **kwargs,
        )
    return outcomes, resends, queued


def _codex_pane_session():
    """A busy codex pane: not at its ready prompt, busy-queue footer up."""
    sess = MagicMock()
    sess.is_at_ready_prompt.return_value = False
    sess.shows_busy_queue_marker.return_value = True
    sess.shows_busy_queue_confirm.return_value = True
    sess.shows_pending_input.return_value = True
    pane = MagicMock()
    pane.session = sess
    pane.model.provider_name = "codex"
    return pane, sess


class TestBusyQueueSubmit:
    """#721: a paste into a busy codex pane must be submitted with the queue
    key (Tab), not Enter, and verified against the queue-confirm markers."""

    def test_queued_message_uses_tab_and_declared_delivered(self) -> None:
        """Marker + queue-confirm both visible → one Tab write, on_queued once,
        settled with queue_submit_used=True and NOT stuck."""
        pane, sess = _codex_pane_session()

        outcomes, resends, queued = _run_inline(pane, sess, provider="codex")

        sess.write.assert_called_once_with(b"\t")
        assert resends == []
        assert queued == [True]
        assert len(outcomes) == 1
        assert outcomes[0].queue_submit_used is True
        assert outcomes[0].stuck_in_composer is False

    def test_stuck_draft_not_confirmed_reports_stuck(self) -> None:
        """Busy budget exhausted with the queue-confirm never appearing → the
        paste is still an unsubmitted draft: settled with stuck_in_composer=True
        (caller must NOT mark it delivered)."""
        pane, sess = _codex_pane_session()
        sess.shows_busy_queue_confirm.return_value = False

        # Small busy budget so the bounded retries are cheap (initial + 3).
        outcomes, resends, queued = _run_inline(pane, sess, provider="codex", busy_max_resends=3)

        assert sess.write.call_count == 1 + 3
        assert all(c.args == (b"\t",) for c in sess.write.call_args_list)
        assert resends == [3, 2, 1]
        assert queued == []
        assert len(outcomes) == 1
        assert outcomes[0].queue_submit_used is True
        assert outcomes[0].stuck_in_composer is True

    def test_no_busy_queue_provider_keeps_enter_and_not_stuck(self) -> None:
        """A provider without a busy-queue marker (claude/agy/opencode) keeps
        the legacy Enter submit and a clean, non-stuck outcome — the busy-queue
        code must not alter the historical no-marker path."""
        sess = MagicMock()
        sess.is_at_ready_prompt.return_value = False
        # Marker accessor exists but reports False (no queue footer on screen).
        sess.shows_busy_queue_marker.return_value = False
        sess.shows_busy_queue_confirm.return_value = False
        pane = MagicMock()
        pane.session = sess
        pane.model.provider_name = "claude"

        outcomes, _resends, queued = _run_inline(pane, sess)

        sess.write.assert_called_once_with(b"\r")
        assert queued == []
        assert len(outcomes) == 1
        assert outcomes[0].queue_submit_used is False
        assert outcomes[0].stuck_in_composer is False


class TestBusyQueueProviderSpec:
    """#721: the busy-queue markers/key are data-layer facts in
    provider_spec.py for codex, and absent (fallback Enter) for every provider
    surveyed without a queue pattern — never hardcoded in the orchestrator."""

    def test_codex_busy_queue_fields_are_wired(self) -> None:
        from agent_takkub.provider_spec import (
            busy_queue_confirm_markers_for,
            busy_queue_key_for,
            busy_queue_marker_for,
        )

        assert busy_queue_marker_for("codex") == "tab to queue message"
        assert busy_queue_key_for("codex") == "\t"
        assert "queued follow-up inputs" in busy_queue_confirm_markers_for("codex")
        assert "messages to be submitted" in busy_queue_confirm_markers_for("codex")

    def test_no_pattern_provider_falls_back_to_enter(self) -> None:
        from agent_takkub.provider_spec import (
            busy_queue_confirm_markers_for,
            busy_queue_key_for,
            busy_queue_marker_for,
        )

        for name in ("claude", "gemini", "agy", "opencode"):
            assert busy_queue_marker_for(name) is None
            assert busy_queue_key_for(name) == "\r"
            assert busy_queue_confirm_markers_for(name) == ()
        # Unknown provider name: same safe fallbacks, never a crash.
        assert busy_queue_marker_for("nope") is None
        assert busy_queue_key_for("nope") == "\r"
        assert busy_queue_confirm_markers_for("nope") == ()
