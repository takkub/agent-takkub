"""Tests for limit-aware auto-resume (🌙) — limit_autoresume.AutoResumeMixin.

Layers:
  1. _usage_confirms_limit — pure signal-(b) check (no Qt).
  2. _maybe_auto_resume_park — the watchdog-tick gate (requires BOTH signals,
     scoped to pending-task panes, respects the cap + re-limit grace).
  3. _on_limit_usage_confirmed / _park_pane_for_limit / _wake_parked_pane —
     the actual park→wake state machine.
  4. set_auto_resume — toggle persist + broadcast.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from agent_takkub import auto_resume
from agent_takkub.limit_autoresume import _usage_confirms_limit
from agent_takkub.limit_status import LimitWindow, UsageData

# ── layer 1: pure signal-(b) check ──────────────────────────────────────────


def _usage(five_hour_pct: float | None) -> UsageData:
    windows = []
    if five_hour_pct is not None:
        windows.append(LimitWindow(name="five_hour", utilization=five_hour_pct, resets_at=None))
    return UsageData(plan="Max", windows=windows, extra_usage_enabled=False)


class TestUsageConfirmsLimit:
    def test_none_usage_not_confirmed(self) -> None:
        assert _usage_confirms_limit(None) is False

    def test_below_threshold_not_confirmed(self) -> None:
        assert _usage_confirms_limit(_usage(50.0)) is False

    def test_at_threshold_confirmed(self) -> None:
        assert _usage_confirms_limit(_usage(95.0)) is True

    def test_above_threshold_confirmed(self) -> None:
        assert _usage_confirms_limit(_usage(99.0)) is True

    def test_no_five_hour_window_not_confirmed(self) -> None:
        usage = UsageData(
            plan="Max",
            windows=[LimitWindow(name="seven_day", utilization=99.0, resets_at=None)],
            extra_usage_enabled=False,
        )
        assert _usage_confirms_limit(usage) is False

    def test_custom_threshold(self) -> None:
        assert _usage_confirms_limit(_usage(80.0), threshold=75.0) is True
        assert _usage_confirms_limit(_usage(80.0), threshold=90.0) is False


# ── shared fixture: a bare Orchestrator with just what AutoResumeMixin touches ──


def _bare_orch():
    from agent_takkub.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._pane_state = {}
    o._panes_by_project = {}
    o.leadInjected = MagicMock()
    o.autoResumeChanged = MagicMock()
    o.limitUsageConfirmed = MagicMock()
    o._notify_lead = MagicMock()
    return o


def _pane_alive():
    p = MagicMock()
    p.session.is_alive = True
    return p


# ── layer 2: _maybe_auto_resume_park gate ───────────────────────────────────


class TestMaybeAutoResumePark:
    def test_disabled_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: False)
        o = _bare_orch()
        o._ps("proj::backend").last_assigned_task = "do the thing"
        o._ps("proj::backend").rate_limited_until = time.time() + 3600
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()
        assert o._ps("proj::backend").limit_confirm_pending is False

    def test_no_pending_task_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        o._ps("proj::backend").rate_limited_until = time.time() + 3600
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()

    def test_no_signal_a_yet_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        o._ps("proj::backend").last_assigned_task = "do the thing"
        # rate_limited_until left at 0.0 — signal (a) not actually recorded.
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()

    def test_signal_a_present_kicks_off_confirm(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_called_once_with("proj", "backend")
        assert ps.limit_confirm_pending is True

    def test_non_claude_shards_park_without_claude_telemetry(self, monkeypatch) -> None:
        from agent_takkub import provider_config

        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        monkeypatch.setattr(provider_config, "_provider_available", lambda provider: True)
        for role in ("codex#2", "gemini#3"):
            o = _bare_orch()
            ps = o._ps(f"proj::{role}")
            ps.last_assigned_task = "do the thing"
            ps.rate_limited_until = time.time() + 3600
            with (
                patch.object(o, "_confirm_limit_via_usage_async") as confirm,
                patch.object(o, "_park_pane_for_limit") as park,
            ):
                o._maybe_auto_resume_park("proj", role, _pane_alive(), time.time())
            confirm.assert_not_called()
            park.assert_called_once_with("proj", role, ps)
            assert ps.limit_confirm_pending is False

    def test_claude_shard_still_uses_claude_telemetry(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        role = "qa#2"
        ps = o._ps(f"proj::{role}")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", role, _pane_alive(), time.time())
        confirm.assert_called_once_with("proj", role)
        assert ps.limit_confirm_pending is True

    def test_already_pending_skips_duplicate_fetch(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_confirm_pending = True
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()

    def test_already_parked_skips(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_parked = True
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()

    def test_already_stopped_skips(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_park_stopped = True
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()

    def test_round_cap_gives_up_without_confirming(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_park_rounds = auto_resume.MAX_PARK_ROUNDS
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), time.time())
        confirm.assert_not_called()
        assert ps.limit_park_stopped is True
        o._notify_lead.assert_called_once()
        assert o._notify_lead.call_args.kwargs["note"] == "round_cap"

    def test_relimit_within_grace_gives_up_without_confirming(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        now = time.time()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = now + 3600
        ps.limit_park_wake_ts = now - 60  # woken 1 minute ago
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), now)
        confirm.assert_not_called()
        assert ps.limit_park_stopped is True
        assert o._notify_lead.call_args.kwargs["note"] == "relimit_within_grace"

    def test_relimit_after_grace_window_proceeds_normally(self, monkeypatch) -> None:
        monkeypatch.setattr(auto_resume, "is_enabled", lambda: True)
        o = _bare_orch()
        now = time.time()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = now + 3600
        ps.limit_park_wake_ts = now - (auto_resume.RELIMIT_GRACE_S + 60)
        with patch.object(o, "_confirm_limit_via_usage_async") as confirm:
            o._maybe_auto_resume_park("proj", "backend", _pane_alive(), now)
        confirm.assert_called_once()
        assert ps.limit_park_stopped is False


# ── layer 3: confirm result → park ──────────────────────────────────────────


class TestOnLimitUsageConfirmed:
    def test_unknown_pane_is_noop(self) -> None:
        o = _bare_orch()
        o._on_limit_usage_confirmed("proj", "backend", True)  # no PaneState registered — no crash

    def test_not_confirmed_stays_notify_only(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_confirm_pending = True
        with patch.object(o, "_park_pane_for_limit") as park:
            o._on_limit_usage_confirmed("proj", "backend", False)
        park.assert_not_called()
        assert ps.limit_confirm_pending is False

    def test_task_finished_meanwhile_skips_park(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_confirm_pending = True
        # last_assigned_task left None — task completed while confirm was in flight.
        with patch.object(o, "_park_pane_for_limit") as park:
            o._on_limit_usage_confirmed("proj", "backend", True)
        park.assert_not_called()

    def test_limit_cleared_meanwhile_skips_park(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.limit_confirm_pending = True
        # rate_limited_until left at 0.0 — the reset window already lifted.
        with patch.object(o, "_park_pane_for_limit") as park:
            o._on_limit_usage_confirmed("proj", "backend", True)
        park.assert_not_called()

    def test_already_parked_skips(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_parked = True
        with patch.object(o, "_park_pane_for_limit") as park:
            o._on_limit_usage_confirmed("proj", "backend", True)
        park.assert_not_called()

    def test_confirmed_parks(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 3600
        ps.limit_confirm_pending = True
        with patch.object(o, "_park_pane_for_limit") as park:
            o._on_limit_usage_confirmed("proj", "backend", True)
        park.assert_called_once_with("proj", "backend", ps)
        assert ps.limit_confirm_pending is False


# ── layer 4: park + wake state machine ──────────────────────────────────────


class TestParkAndWake:
    def test_park_notifies_lead_and_schedules_wake(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.rate_limited_until = time.time() + 3600
        with patch("agent_takkub.limit_autoresume.QTimer.singleShot") as timer:
            o._park_pane_for_limit("proj", "backend", ps)
        assert ps.limit_parked is True
        assert ps.limit_park_rounds == 1
        o._notify_lead.assert_called_once()
        assert o._notify_lead.call_args.kwargs["note"] == "limit_parked"
        timer.assert_called_once()

    def test_wake_unknown_pane_state_is_noop(self) -> None:
        o = _bare_orch()
        o._wake_parked_pane("proj", "backend")  # no PaneState — no crash

    def test_wake_not_parked_is_noop(self) -> None:
        o = _bare_orch()
        o._ps("proj::backend").limit_parked = False
        o._wake_parked_pane("proj", "backend")
        # nothing to assert beyond "did not raise" — guarded no-op

    def test_wake_pane_gone_clears_park_flag(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        ps.last_assigned_task = "do the thing"
        # _panes_by_project has no "proj" project at all → pane is None
        with patch("agent_takkub.limit_autoresume._log_event") as log:
            o._wake_parked_pane("proj", "backend")
        assert ps.limit_parked is False
        assert any(c.args[0] == "pane_limit_wake_skipped" for c in log.call_args_list)

    def test_wake_task_already_done_clears_park_flag(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        # last_assigned_task left None — pane already finished/reassigned.
        o._panes_by_project["proj"] = {"backend": _pane_alive()}
        with patch("agent_takkub.limit_autoresume._delayed_enter") as inject:
            o._wake_parked_pane("proj", "backend")
        assert ps.limit_parked is False
        inject.assert_not_called()

    def test_wake_happy_path_injects_and_notifies(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        ps.limit_park_rounds = 1
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 100
        pane = _pane_alive()
        o._panes_by_project["proj"] = {"backend": pane}

        before = time.time()
        with patch("agent_takkub.limit_autoresume._delayed_enter") as inject:
            o._wake_parked_pane("proj", "backend")
        after = time.time()

        assert ps.limit_parked is False
        assert ps.rate_limited_until == 0.0
        assert before <= ps.limit_park_wake_ts <= after
        pane.session.write.assert_called_once()
        inject.assert_called_once()
        o._notify_lead.assert_called_once()
        assert o._notify_lead.call_args.kwargs["note"] == "limit_resumed"


# ── layer 5: toggle ──────────────────────────────────────────────────────────


class TestSetAutoResume:
    def test_enable_persists_and_broadcasts(self, monkeypatch) -> None:
        o = _bare_orch()
        saved = {}
        monkeypatch.setattr(auto_resume, "set_enabled", lambda flag: saved.setdefault("v", flag))
        lead = _pane_alive()
        o._panes_by_project["proj"] = {"lead": lead}
        from agent_takkub.roles import LEAD

        o._panes_by_project["proj"][LEAD.name] = lead
        ok, _msg = o.set_auto_resume(True)
        assert ok is True
        assert saved["v"] is True
        lead.session.write.assert_called()
        o.autoResumeChanged.emit.assert_called_once_with(True)

    def test_disable_persists_and_broadcasts(self, monkeypatch) -> None:
        o = _bare_orch()
        saved = {}
        monkeypatch.setattr(auto_resume, "set_enabled", lambda flag: saved.setdefault("v", flag))
        ok, _msg = o.set_auto_resume(False)
        assert ok is True
        assert saved["v"] is False
        o.autoResumeChanged.emit.assert_called_once_with(False)
