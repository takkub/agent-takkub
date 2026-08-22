"""Tests for cross-provider quota/usage-limit stall detection (#301) and the
heartbeat "output vs real progress" split it depends on (#308).

Layers:
  1. provider_spec.quota_markers_for — per-provider marker tables.
  2. pty_session pure parsers — duration ("resets in XhYmZs") vs clock-time
     ("resets 3pm") reset parsing, PtySession.rate_limit_reset_at()/
     quota_stall_marker()/current_model_label() provider-aware wrappers.
  3. Heartbeat: _content_fingerprint ignores a live elapsed-seconds counter
     (#308's actual root cause — a growing "412s" defeated spinner-only
     normalization even though the busy phrase itself never changed).
  4. Orchestrator: _derive_display_state's "stalled:quota" priority tier,
     _rate_limit_suppressed capturing provider/marker + firing the immediate
     Lead notice, and _check_stuck_panes's spinner-phrase filter catching a
     trailing elapsed counter ("esc to cancel · 412s)") it used to miss.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import quota_markers_for
from agent_takkub.pty_session import (
    PtySession,
    _content_fingerprint,
    _parse_duration_reset,
    _parse_rate_limit_reset,
)
from tests.test_lifecycle_recovery import _check_stuck, _FakeOrchForContentDelta

# ── layer 1: per-provider marker tables ─────────────────────────────────────


class TestQuotaMarkersFor:
    def test_gemini_has_confirmed_marker(self) -> None:
        assert "individual quota reached" in quota_markers_for("gemini")

    def test_codex_has_reached_state_markers_only(self) -> None:
        markers = quota_markers_for("codex")
        assert "rate limit reached" in markers
        # Bare topic words must never appear — the exact false-positive class
        # GENERIC_QUOTA_MARKERS's own module note documents (claude's Fable-5
        # promo banner false-positived on the bare phrase "usage limit").
        assert "rate limit" not in markers
        assert "usage limit" not in markers

    def test_unconfirmed_providers_fall_back_to_generic_only(self) -> None:
        for provider in ("opencode", "kimi", "cursor"):
            markers = quota_markers_for(provider)
            assert "limit reached" in markers  # generic baseline still applies
            assert "individual quota reached" not in markers  # gemini-only

    def test_unknown_provider_gets_generic_only(self) -> None:
        assert quota_markers_for("nonexistent-provider") == quota_markers_for("kimi")


# ── layer 2: duration parsing + PtySession wrappers ─────────────────────────


class TestDurationReset:
    NOW = 1_700_000_000.0

    def test_hms_duration_parsed(self) -> None:
        epoch = _parse_duration_reset("resets in 1h53m57s", self.NOW)
        assert epoch == pytest.approx(self.NOW + 3600 + 53 * 60 + 57)

    def test_minutes_only_duration(self) -> None:
        epoch = _parse_duration_reset("resets in 45m", self.NOW)
        assert epoch == pytest.approx(self.NOW + 45 * 60)

    def test_no_duration_phrase_returns_none(self) -> None:
        assert _parse_duration_reset("resets 3pm", self.NOW) is None

    def test_empty_duration_returns_none(self) -> None:
        # "resets in" with no digits after it must not silently mean 0s.
        assert _parse_duration_reset("resets in", self.NOW) is None


class TestParseRateLimitResetWithProviderMarkers:
    NOW = 1_700_000_000.0

    def test_gemini_banner_full_text(self) -> None:
        banner = (
            "⚠ individual quota reached. please upgrade your subscription "
            "to increase your limits. resets in 1h53m57s."
        )
        epoch = _parse_rate_limit_reset(banner, self.NOW, quota_markers_for("gemini"))
        assert epoch == pytest.approx(self.NOW + 3600 + 53 * 60 + 57)

    def test_gemini_markers_do_not_match_codex_only_banner(self) -> None:
        # "you've hit your rate limit" is codex-only wording that doesn't
        # overlap any GENERIC_QUOTA_MARKERS phrase either — a gemini pane
        # showing it (never happens in practice) must not false-positive.
        assert (
            _parse_rate_limit_reset(
                "you've hit your rate limit", self.NOW, quota_markers_for("gemini")
            )
            is None
        )

    def test_codex_reached_banner_uses_fallback_when_no_reset_time(self) -> None:
        epoch = _parse_rate_limit_reset(
            "you've hit your rate limit", self.NOW, quota_markers_for("codex")
        )
        assert epoch is not None
        assert epoch > self.NOW

    def test_markers_none_falls_back_to_env_overridable_default(self) -> None:
        # Back-compat: existing 2-arg call sites (claude's own pinned tests)
        # must keep working unchanged.
        assert _parse_rate_limit_reset("just a normal ready prompt", self.NOW) is None
        assert _parse_rate_limit_reset("usage limit reached. resets 3pm", self.NOW) is not None


class TestPtySessionQuotaWrappers:
    def test_rate_limit_reset_at_provider_aware(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Individual quota reached. Resets in 1h53m57s.")
        assert s.rate_limit_reset_at("gemini") is not None
        # claude's table doesn't include gemini's phrase.
        s2 = PtySession(cols=80, rows=24)
        s2._feed_and_log(b"Individual quota reached. Resets in 1h53m57s.")
        assert s2.rate_limit_reset_at("claude") is None

    def test_quota_stall_marker_reports_matched_phrase(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Individual quota reached. Resets in 1h53m57s.")
        assert s.quota_stall_marker("gemini") == "individual quota reached"

    def test_quota_stall_marker_none_when_not_hit(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"? for shortcuts")
        assert s.quota_stall_marker("gemini") is None

    def test_current_model_label_gemini(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"> \n? for shortcuts            Gemini 3.5 Flash (Medium)")
        label = s.current_model_label("gemini")
        assert label is not None
        assert "Gemini 3.5 Flash" in label

    def test_current_model_label_none_for_uncalibrated_provider(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Gemini 3.5 Flash (Medium)")
        assert s.current_model_label("codex") is None


# ── layer 3: heartbeat — elapsed counter must not look like progress ───────


class TestContentFingerprintIgnoresElapsedCounter:
    def test_growing_counter_alone_does_not_change_fingerprint(self) -> None:
        # #308: a live "(esc to cancel · 412s)" counter changed every second
        # even though the pane was genuinely frozen — the fingerprint must
        # treat consecutive ticks of the SAME busy line (only the counter
        # incrementing) as unchanged, the same way it already treats a
        # rotating spinner glyph as unchanged.
        a = _content_fingerprint(["Running command... (esc to cancel · 412s)"])
        b = _content_fingerprint(["Running command... (esc to cancel · 413s)"])
        assert a == b
        assert a != ""

    def test_counter_with_trailing_paren_still_stripped(self) -> None:
        # The narrower regression: the old regex required a trailing
        # space/middot after the digits, so "...412s)" (paren right after)
        # never matched at all.
        assert _content_fingerprint(["(esc to cancel · 5s)"]) == _content_fingerprint(
            ["(esc to cancel · 500s)"]
        )

    def test_real_text_change_still_registers(self) -> None:
        assert _content_fingerprint(["Running command..."]) != _content_fingerprint(["Done."])


# ── layer 4: orchestrator — display_state, notify, stuck-detector filter ───


class TestDisplayStateQuotaPriority:
    def test_quota_stalled_wins_over_login_required(self) -> None:
        pane = MagicMock()
        pane.session.auth_failure_reason.return_value = "send /login to login"
        pane.model.provider_name = "kimi"
        result = Orchestrator._derive_display_state(
            None, pane, "working", False, quota_stalled=True
        )
        assert result == "stalled:quota"
        # auth_failure_reason must never even be consulted once quota_stalled
        # is already known True — screen-scraping again risks disagreeing
        # with the watchdog's own already-recorded verdict.
        pane.session.auth_failure_reason.assert_not_called()

    def test_not_quota_stalled_falls_through_normally(self) -> None:
        pane = MagicMock()
        pane.session.account_pending_reason.return_value = None
        pane.session.auth_failure_reason.return_value = None
        pane.session.shows_boot_phase_marker.return_value = False
        pane.model.provider_name = "claude"
        result = Orchestrator._derive_display_state(
            None, pane, "working", False, quota_stalled=False
        )
        assert result == "working"

    def test_default_quota_stalled_is_false_backward_compat(self) -> None:
        # Existing 4-positional-arg call sites (pre-#301 tests) must be
        # unaffected by the new parameter's default.
        pane = MagicMock()
        pane.session.account_pending_reason.return_value = None
        pane.session.auth_failure_reason.return_value = None
        pane.session.shows_boot_phase_marker.return_value = False
        pane.session.is_hard_blocked_for.return_value = False
        pane.model.provider_name = "claude"
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "active"


def _bare_orch_with_notify():
    o = Orchestrator.__new__(Orchestrator)
    o._pane_state = {}
    o._notify_lead = MagicMock()
    return o


def _quota_pane(reset_at: float, marker: str = "individual quota reached", model=None):
    pane = MagicMock()
    pane.session.is_alive = True
    pane.session.rate_limit_reset_at.return_value = reset_at
    pane.session.is_at_limit_choice_modal.return_value = False
    pane.session.quota_stall_marker.return_value = marker
    pane.session.current_model_label.return_value = model
    pane.model.provider_name = "gemini"
    return pane


class TestRateLimitSuppressedNotifiesImmediately:
    def test_detection_calls_notify_with_provider_and_role(self) -> None:
        o = _bare_orch_with_notify()
        now = time.time()
        pane = _quota_pane(now + 6000)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            suppressed = o._rate_limit_suppressed("proj", "frontend", pane, now)
        assert suppressed is True
        o._notify_lead.assert_called_once()
        call_project, msg = o._notify_lead.call_args[0]
        assert call_project == "proj"
        assert "[system]" in msg
        assert "frontend" in msg
        assert "gemini" in msg
        assert "individual quota reached" in msg
        ps = o._pane_state["proj::frontend"]
        assert ps.quota_marker == "individual quota reached"
        assert ps.quota_provider == "gemini"

    def test_model_downgrade_surfaced_in_notice(self) -> None:
        o = _bare_orch_with_notify()
        now = time.time()
        pane = _quota_pane(now + 6000, model="Gemini 3.5 Flash (Medium)")
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            o._rate_limit_suppressed("proj", "frontend", pane, now)
        _, msg = o._notify_lead.call_args[0]
        assert "Gemini 3.5 Flash" in msg

    def test_second_tick_does_not_renotify(self) -> None:
        o = _bare_orch_with_notify()
        now = time.time()
        pane = _quota_pane(now + 6000)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            o._rate_limit_suppressed("proj", "qa", pane, now)
            o._rate_limit_suppressed("proj", "qa", pane, now + 5)
        assert o._notify_lead.call_count == 1

    def test_notify_failure_does_not_break_the_gate(self) -> None:
        # notify is best-effort — a broken Lead-delivery path must never
        # take down quota detection/suppression itself.
        o = _bare_orch_with_notify()
        o._notify_lead.side_effect = RuntimeError("boom")
        now = time.time()
        pane = _quota_pane(now + 6000)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            assert o._rate_limit_suppressed("proj", "qa", pane, now) is True


class TestStuckDetectorCatchesAgyElapsedCounter:
    """#308: agy's own busy phrase is "esc to cancel" (confirmed by the
    issue itself), missing from the old hand-copied 4-item phrase list, and
    a trailing "412s)" counter defeated the volatile-counter regex too. Both
    gaps together made a genuinely wedged agy pane look like it was still
    making progress every tick."""

    def test_esc_to_cancel_line_with_trailing_counter_detected_as_stuck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = now - 1
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_blocked_on_permission_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        # Real #308 shape: agy's own busy phrase + a trailing elapsed
        # counter with no space/middot before the closing paren.
        sess.display_lines.return_value = ["Running command... (esc to cancel · 412s)"]
        pane.session = sess

        fake._panes_by_project["p"] = {"backend": pane}

        key = "p::backend"
        import hashlib

        empty_hash = hashlib.blake2b(b"", digest_size=8).hexdigest()
        fake._ps(key).last_content_hash = empty_hash
        from agent_takkub.orchestrator import STUCK_THRESHOLD_S

        fake._ps(key).last_content_change_ts = now - STUCK_THRESHOLD_S - 5

        _check_stuck(fake, now)
        assert fake.recover_calls == [("backend", "p")]
