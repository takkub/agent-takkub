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
        for banner in ("usage limit · resets 1am", "usage limit · resets 11pm"):
            epoch = _parse_rate_limit_reset(banner, self.NOW)
            assert epoch is not None and epoch > self.NOW

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


def _pane_reporting(reset_at):
    pane = MagicMock()
    pane.session.is_alive = True
    pane.session.rate_limit_reset_at.return_value = reset_at
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
