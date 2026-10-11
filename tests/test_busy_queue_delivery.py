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

    def test_steer_delivery_presses_enter_first_809(self) -> None:
        """#809 (codex 0.160 live: Enter mid-turn = steer into the running
        turn) — a steer delivery submits with Enter, confirmed by the same
        "Messages to be submitted" marker."""
        pane, sess = _codex_pane_session()

        outcomes, _resends, queued = _run_inline(pane, sess, provider="codex", steer=True)

        sess.write.assert_called_once_with(b"\r")
        assert queued == []
        assert outcomes[0].queue_submit_used is False
        assert outcomes[0].stuck_in_composer is False

    def test_steer_retries_never_demote_a_correction_to_the_turn_end_queue_841(self) -> None:
        """A delayed confirmation must not change an instruction's delivery mode."""
        pane, sess = _codex_pane_session()
        sess.shows_busy_queue_confirm.side_effect = [False, True]

        outcomes, resends, _queued = _run_inline(
            pane, sess, provider="codex", steer=True, busy_max_resends=3
        )

        assert [c.args for c in sess.write.call_args_list] == [(b"\r",), (b"\r",)]
        assert resends == [3]
        assert outcomes[0].stuck_in_composer is False

    def test_existing_queue_chrome_does_not_confirm_a_new_draft_841(self) -> None:
        pane, sess = _codex_pane_session()
        # The old queue is still visible while this correction remains in the composer.
        outcomes, _resends, queued = _run_inline(
            pane,
            sess,
            provider="codex",
            steer=True,
            busy_max_resends=2,
            payload="new correction",
            content_fragment="new correction",
            repaste=False,
        )
        assert all(call.args == (b"\r",) for call in sess.write.call_args_list)
        assert len(sess.write.call_args_list) == 3
        assert queued == []
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

        assert busy_queue_marker_for("gemini") == "press up to edit queued messages"
        assert busy_queue_key_for("gemini") == "\r"
        for name in ("claude", "agy", "opencode"):
            assert busy_queue_marker_for(name) is None
            assert busy_queue_key_for(name) == "\r"
            assert busy_queue_confirm_markers_for(name) == ()
        # Unknown provider name: same safe fallbacks, never a crash.
        assert busy_queue_marker_for("nope") is None
        assert busy_queue_key_for("nope") == "\r"
        assert busy_queue_confirm_markers_for("nope") == ()


def test_real_terminal_screen_distinguishes_existing_queue_from_new_correction():
    """Exercise actual PTY screen probes with both queue chrome and a draft."""
    from agent_takkub.pty_session import PtySession

    session = PtySession(cols=120, rows=24)

    def redraw(*lines):
        session._feed_and_log(("\x1b[2J\x1b[H" + "\r\n".join(lines)).encode())

    redraw(
        "Working (12s · esc to interrupt)",
        "Queued follow-up inputs",
        "  Old instruction",
        "› New correction",
        "esc to interrupt · tab to queue message",
    )
    assert session.shows_busy_queue_confirm("codex")
    assert session.shows_pending_input("New correction")

    writes = []

    def write(key):
        writes.append(key)
        if len(writes) == 2:
            redraw(
                "Working (12s · esc to interrupt)",
                "Messages to be submitted after next tool call",
                "  New correction",
                "› ",
                "esc to interrupt · tab to queue message",
            )
        return True

    session.write = write
    pane = MagicMock()
    pane.session = session
    pane.model.provider_name = "codex"
    outcomes, _resends, queued = _run_inline(
        pane,
        session,
        provider="codex",
        steer=True,
        busy_max_resends=2,
        payload="New correction",
        content_fragment="New correction",
        repaste=False,
    )
    assert writes == [b"\r", b"\r"]
    assert not queued
    assert not outcomes[0].stuck_in_composer


def test_codex_queue_confirm_matches_edit_hint_when_header_scrolled_off_843() -> None:
    from agent_takkub.pty_session import PtySession

    sess = PtySession.__new__(PtySession)
    lines = ["• earlier output"] * 30 + [
        "  ↳ second queued message",
        "    shift+← edit last queued message",
        "",
        "› ",
        "",
        "  gpt-6.1 · 42% context left",
    ]
    sess.display_lines = lambda: lines  # type: ignore[method-assign]
    assert sess.shows_busy_queue_confirm("codex")
