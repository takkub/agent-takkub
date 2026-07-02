"""Tests for usage-limit (rate-limit) watchdog suppression.

Two layers:
  1. _parse_rate_limit_reset — pure banner→reset-epoch parser (no Qt).
  2. Orchestrator._rate_limit_suppressed — the watchdog gate that records the
     reset time, schedules a notice, and suppresses the idle/stuck loops.

The exact banner wording is Claude-version-dependent (see pty_session module
notes); these tests pin the *logic*, not the real marker string, so flipping
the marker later doesn't churn them.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from agent_takkub.orchestrator import PaneState
from agent_takkub.pty_session import _parse_rate_limit_reset

# ── layer 1: pure parser ─────────────────────────────────────────────────────


class TestParseRateLimitReset:
    NOW = 1_700_000_000.0  # fixed reference epoch

    def test_no_banner_returns_none(self) -> None:
        assert _parse_rate_limit_reset("just a normal ready prompt", self.NOW) is None

    def test_banner_with_pm_time(self) -> None:
        epoch = _parse_rate_limit_reset("usage limit reached. resets 3pm", self.NOW)
        assert epoch is not None
        assert time.localtime(epoch).tm_hour == 15
        assert time.localtime(epoch).tm_min == 0

    def test_banner_with_hhmm_24h(self) -> None:
        epoch = _parse_rate_limit_reset("limit reached — reset at 14:30", self.NOW)
        assert epoch is not None
        assert time.localtime(epoch).tm_hour == 14
        assert time.localtime(epoch).tm_min == 30

    def test_banner_without_time_uses_5h_fallback(self) -> None:
        epoch = _parse_rate_limit_reset("you've reached your usage limit", self.NOW)
        assert epoch is not None
        assert abs(epoch - (self.NOW + 5 * 3600)) < 1

    def test_reset_time_is_always_in_future(self) -> None:
        # Whatever clock time is parsed, the epoch must be after `now`
        # (today if still ahead, else tomorrow).
        for banner in ("usage limit reached · resets 1am", "usage limit reached · resets 11pm"):
            epoch = _parse_rate_limit_reset(banner, self.NOW)
            assert epoch is not None and epoch > self.NOW

    def test_promo_notice_is_not_a_limit_banner(self) -> None:
        # Claude Code v2.1.198 Fable-5 promo shows on EVERY fresh pane and
        # merely *talks about* limits — it must not flag the pane as
        # rate-limited. The false flag suppressed the idle watchdog for 5 h
        # and starved the rescue of swallowed task submits (QA fan-out
        # stuck-paste incident, 2026-07-02).
        promo = (
            "fable 5 is back.\n"
            "until july 7, you can use up to 50% of your plan's weekly usage "
            "limit on fable 5. if you hit your limit, you can continue on "
            "fable 5 after it resets."
        )
        assert _parse_rate_limit_reset(promo, self.NOW) is None

    def test_real_banners_still_detected(self) -> None:
        # The tightened markers must keep matching genuine reached-state
        # banners across known claude wordings.
        for banner in (
            "claude usage limit reached. your limit will reset at 11pm",
            "you've reached your usage limit",
            "you've hit your usage limit — upgrade or wait",
            "5-hour limit reached ∙ resets 3pm",
            "you've hit your weekly limit · resets 8am",
        ):
            assert _parse_rate_limit_reset(banner, self.NOW) is not None, banner

    def test_session_limit_banner_v2_1_198(self) -> None:
        # Field-verified wording (screenshot 2026-07-02): the session-limit
        # banner says "session limit", not "usage limit" — the marker set must
        # match it AND parse the hh:mm reset time.
        epoch = _parse_rate_limit_reset(
            "you've hit your session limit · resets 1:10pm (asia/bangkok)\n"
            "/upgrade to increase your usage limit.",
            self.NOW,
        )
        assert epoch is not None
        assert time.localtime(epoch).tm_hour == 13
        assert time.localtime(epoch).tm_min == 10

    def test_12am_maps_to_midnight(self) -> None:
        epoch = _parse_rate_limit_reset("usage limit reached, resets 12am", self.NOW)
        assert epoch is not None
        assert time.localtime(epoch).tm_hour == 0

    def test_env_marker_override(self, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_RATE_LIMIT_MARKERS", "quota exhausted,no credits")
        assert _parse_rate_limit_reset("default usage limit text", self.NOW) is None
        assert _parse_rate_limit_reset("quota exhausted, resets 9am", self.NOW) is not None


# ── layer 2: orchestrator gate ───────────────────────────────────────────────


def _bare_orch():
    """Orchestrator with just the attributes _rate_limit_suppressed touches."""
    from agent_takkub.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._pane_state = {}
    return o


def _pane_reporting(reset_at, *, at_choice_modal: bool = False):
    pane = MagicMock()
    pane.session.is_alive = True
    pane.session.rate_limit_reset_at.return_value = reset_at
    pane.session.is_at_limit_choice_modal.return_value = at_choice_modal
    return pane


class TestRateLimitGate:
    def test_detects_and_suppresses(self) -> None:
        o = _bare_orch()
        now = time.time()
        pane = _pane_reporting(now + 3600)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot") as timer,
            patch("agent_takkub.orchestrator._log_event"),
        ):
            suppressed = o._rate_limit_suppressed("proj", "frontend", pane, now)
        assert suppressed is True
        assert (o._pane_state.get("proj::frontend") or PaneState()).rate_limited_until == now + 3600
        timer.assert_called_once()  # reset notice scheduled

    def test_choice_modal_confirmed_once_on_detection(self) -> None:
        # v2.1.198 pairs the banner with a chooser whose preselected option 1
        # is "Stop and wait for limit to reset" — first detection must confirm
        # it with a single Enter so the pane waits instead of blocking.
        o = _bare_orch()
        now = time.time()
        pane = _pane_reporting(now + 3600, at_choice_modal=True)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event") as log,
        ):
            assert o._rate_limit_suppressed("proj", "qa", pane, now) is True
            # Second tick: known-limited short-circuit — no second Enter.
            assert o._rate_limit_suppressed("proj", "qa", pane, now + 5) is True
        pane.session.write.assert_called_once_with(b"\r")
        assert "rate_limit_modal_confirmed" in [c.args[0] for c in log.call_args_list]

    def test_no_modal_no_enter(self) -> None:
        o = _bare_orch()
        now = time.time()
        pane = _pane_reporting(now + 3600, at_choice_modal=False)
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            assert o._rate_limit_suppressed("proj", "qa", pane, now) is True
        pane.session.write.assert_not_called()

    def test_not_limited_returns_false(self) -> None:
        o = _bare_orch()
        now = time.time()
        pane = _pane_reporting(None)
        with patch("agent_takkub.orchestrator._log_event"):
            assert o._rate_limit_suppressed("proj", "backend", pane, now) is False
        assert (o._pane_state.get("proj::backend") or PaneState()).rate_limited_until == 0.0

    def test_known_limit_skips_redetect(self) -> None:
        o = _bare_orch()
        now = time.time()
        o._ps("proj::qa").rate_limited_until = now + 1800
        pane = _pane_reporting(None)  # would say "not limited" if asked
        assert o._rate_limit_suppressed("proj", "qa", pane, now) is True
        pane.session.rate_limit_reset_at.assert_not_called()  # short-circuited

    def test_reset_time_passed_clears_and_resumes(self) -> None:
        o = _bare_orch()
        now = time.time()
        o._ps("proj::qa").rate_limited_until = now - 10  # already reset
        pane = _pane_reporting(None)
        assert o._rate_limit_suppressed("proj", "qa", pane, now) is False
        assert (o._pane_state.get("proj::qa") or PaneState()).rate_limited_until == 0.0

    def test_dead_session_not_limited(self) -> None:
        o = _bare_orch()
        now = time.time()
        pane = MagicMock()
        pane.session.is_alive = False
        with patch("agent_takkub.orchestrator._log_event"):
            assert o._rate_limit_suppressed("proj", "devops", pane, now) is False


# ── layer 3: _emit_rate_limit_reset guards ───────────────────────────────────


def _bare_emit_orch():
    """Orchestrator with attributes needed by _emit_rate_limit_reset."""
    from agent_takkub.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._pane_state = {}
    o._panes_by_project = {}
    o.leadInjected = MagicMock()
    o.statusChanged = MagicMock()
    return o


def _alive_pane():
    p = MagicMock()
    p.session.is_alive = True
    return p


def _dead_pane():
    p = MagicMock()
    p.session.is_alive = False
    return p


class TestEmitRateLimitReset:
    def test_pane_gone_skips_lead_notice(self) -> None:
        """Timer fires after pane closed → Lead must NOT receive the message."""
        o = _bare_emit_orch()
        o._ps("proj::backend").rate_limited_until = time.time() + 1
        # pane not registered → _project_panes("proj").get("backend") is None
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event") as log,
        ):
            o._emit_rate_limit_reset("proj", "backend")
        o.leadInjected.emit.assert_not_called()
        o.statusChanged.emit.assert_not_called()
        skipped_calls = [c for c in log.call_args_list if c.args[0] == "rate_limit_reset_skipped"]
        assert skipped_calls, "expected rate_limit_reset_skipped event"
        assert skipped_calls[0].kwargs.get("reason") == "pane_gone"
        # state cleared even though notice was skipped
        assert o._pane_state["proj::backend"].rate_limited_until == 0.0

    def test_pane_dead_session_skips_lead_notice(self) -> None:
        """Pane exists but session is dead → skip."""
        o = _bare_emit_orch()
        o._ps("proj::qa").rate_limited_until = time.time() + 1
        o._project_panes("proj")["qa"] = _dead_pane()
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event") as log,
        ):
            o._emit_rate_limit_reset("proj", "qa")
        o.leadInjected.emit.assert_not_called()
        skipped = [c for c in log.call_args_list if c.args[0] == "rate_limit_reset_skipped"]
        assert skipped and skipped[0].kwargs.get("reason") == "pane_gone"

    def test_pane_alive_injects_and_resets_ts(self) -> None:
        """Pane alive → Lead gets notice AND last_content_change_ts reset (#53)."""
        o = _bare_emit_orch()
        reset_at = time.time() + 1
        o._ps("proj::frontend").rate_limited_until = reset_at
        o._ps("proj::frontend").last_content_change_ts = 0.0

        alive_role_pane = _alive_pane()
        alive_lead_pane = _alive_pane()
        panes = o._project_panes("proj")
        panes["frontend"] = alive_role_pane

        from agent_takkub.roles import LEAD

        panes[LEAD.name] = alive_lead_pane

        before = time.time()
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event") as log,
        ):
            o._emit_rate_limit_reset("proj", "frontend")
        after = time.time()

        alive_lead_pane.session.write.assert_called_once()
        o.leadInjected.emit.assert_called_once()
        o.statusChanged.emit.assert_called_once()
        ps = o._pane_state["proj::frontend"]
        assert ps.rate_limited_until == 0.0
        assert before <= ps.last_content_change_ts <= after  # #53 preserved
        # must have logged rate_limit_reset (not skipped)
        logged = [c.args[0] for c in log.call_args_list]
        assert "rate_limit_reset" in logged
        assert "rate_limit_reset_skipped" not in logged

    def test_duplicate_timer_injects_only_once(self) -> None:
        """Two timers fire for same episode → only the first injects."""
        o = _bare_emit_orch()
        o._ps("proj::mobile").rate_limited_until = time.time() + 1

        alive_role_pane = _alive_pane()
        alive_lead_pane = _alive_pane()
        panes = o._project_panes("proj")
        panes["mobile"] = alive_role_pane

        from agent_takkub.roles import LEAD

        panes[LEAD.name] = alive_lead_pane

        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            o._emit_rate_limit_reset("proj", "mobile")  # first fire
            o._emit_rate_limit_reset("proj", "mobile")  # duplicate — must be skipped

        assert alive_lead_pane.session.write.call_count == 1
        assert o.leadInjected.emit.call_count == 1
