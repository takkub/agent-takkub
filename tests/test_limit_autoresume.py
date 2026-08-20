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

import json
import time
from unittest.mock import MagicMock, patch

from agent_takkub import auto_resume
from agent_takkub.limit_autoresume import (
    _pane_cwd,
    _pane_output_tail,
    _progress_marker_path,
    _usage_confirms_limit,
    _write_progress_marker,
)
from agent_takkub.limit_status import LimitWindow, UsageData
from agent_takkub.spawn_engine import PaneState

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

    def test_unknown_utilization_not_confirmed(self) -> None:
        """utilization=None (API omitted the figure) must never confirm a
        limit — unknown is not 'exhausted'."""
        usage = UsageData(
            plan="Max",
            windows=[LimitWindow(name="five_hour", utilization=None, resets_at=None)],
            extra_usage_enabled=False,
        )
        assert _usage_confirms_limit(usage) is False


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

    def test_wake_skips_nudge_when_cli_already_auto_continued(self) -> None:
        # #322: Claude Code 2.1.234+ auto-continues the interrupted turn on
        # its own once the window resets. If our WAKE_BUFFER_S-delayed timer
        # fires and the banner is already gone, the pane resumed itself —
        # writing our own nudge + Enter on top of that would race live
        # generation (A3 draft-hold-style race). Simulated via
        # rate_limit_reset_at() returning None (banner cleared).
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        ps.limit_park_rounds = 1
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 100
        ps.quota_provider = "claude"
        pane = _pane_alive()
        pane.session.rate_limit_reset_at.return_value = None  # banner already cleared
        o._panes_by_project["proj"] = {"backend": pane}

        with patch("agent_takkub.limit_autoresume._delayed_enter") as inject:
            o._wake_parked_pane("proj", "backend")

        assert ps.limit_parked is False
        assert ps.rate_limited_until == 0.0
        pane.session.write.assert_not_called()
        inject.assert_not_called()
        pane.session.rate_limit_reset_at.assert_called_once_with("claude")
        o._notify_lead.assert_called_once()
        assert o._notify_lead.call_args.kwargs["note"] == "limit_resumed_self"

    def test_wake_still_limited_falls_back_to_legacy_nudge(self) -> None:
        # Counterpart of the above: banner still showing at wake time (the
        # pre-2.1.234 "press enter to continue" CLI, or a still-blocked
        # session) must keep sending the manual nudge exactly as before.
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 100
        ps.quota_provider = "claude"
        pane = _pane_alive()
        pane.session.rate_limit_reset_at.return_value = time.time() + 10  # still showing
        o._panes_by_project["proj"] = {"backend": pane}

        with patch("agent_takkub.limit_autoresume._delayed_enter") as inject:
            o._wake_parked_pane("proj", "backend")

        pane.session.write.assert_called_once()
        inject.assert_called_once()
        assert o._notify_lead.call_args.kwargs["note"] == "limit_resumed"

    def test_wake_recheck_error_fails_safe_to_legacy_nudge(self) -> None:
        # A broken re-check (torn-down session, unexpected exception) must
        # never silently strand a parked pane — fail open to the legacy
        # nudge path rather than swallowing the wake.
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.limit_parked = True
        ps.last_assigned_task = "do the thing"
        ps.rate_limited_until = time.time() + 100
        pane = _pane_alive()
        pane.session.rate_limit_reset_at.side_effect = RuntimeError("boom")
        o._panes_by_project["proj"] = {"backend": pane}

        with patch("agent_takkub.limit_autoresume._delayed_enter") as inject:
            o._wake_parked_pane("proj", "backend")

        pane.session.write.assert_called_once()
        inject.assert_called_once()
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


# ── layer 6: give-up status dump (#158) ─────────────────────────────────────


class TestPaneCwd:
    def test_none_pane_returns_none(self) -> None:
        assert _pane_cwd(None) is None

    def test_missing_attr_returns_none(self) -> None:
        pane = MagicMock(spec=[])  # no _session_cwd attribute at all
        assert _pane_cwd(pane) is None

    def test_empty_cwd_returns_none(self) -> None:
        pane = MagicMock()
        pane._session_cwd = ""
        assert _pane_cwd(pane) is None

    def test_real_cwd_returned(self) -> None:
        pane = MagicMock()
        pane._session_cwd = "C:/work/api"
        assert _pane_cwd(pane) == "C:/work/api"


class TestPaneOutputTail:
    def test_none_pane_returns_empty(self) -> None:
        assert _pane_output_tail(None) == ""

    def test_none_session_returns_empty(self) -> None:
        pane = MagicMock()
        pane.session = None
        assert _pane_output_tail(pane) == ""

    def test_display_lines_error_returns_empty(self) -> None:
        pane = MagicMock()
        pane.session.display_lines.side_effect = RuntimeError("boom")
        assert _pane_output_tail(pane) == ""

    def test_blank_lines_dropped_and_trailing_kept(self) -> None:
        pane = MagicMock()
        pane.session.display_lines.return_value = ["a", "", "  ", "b", "c"]
        assert _pane_output_tail(pane, max_lines=2) == "b\nc"

    def test_max_lines_default_from_auto_resume_constant(self) -> None:
        pane = MagicMock()
        pane.session.display_lines.return_value = [f"line{i}" for i in range(20)]
        tail = _pane_output_tail(pane)
        assert tail.count("\n") + 1 == auto_resume.GIVE_UP_TAIL_LINES
        assert tail.splitlines()[-1] == "line19"


class TestProgressMarkerPath:
    def test_creates_project_dir_and_role_file_name(self) -> None:
        path = _progress_marker_path("proj", "backend")
        assert path.name == "backend.json"
        assert path.parent.name == "proj"
        assert path.parent.is_dir()


class TestWriteProgressMarker:
    def test_writes_expected_fields(self) -> None:
        ps = PaneState()
        ps.last_assigned_task = "fix the thing"
        ps.last_assigned_task_file = "/tmp/task.txt"
        ps.limit_park_rounds = 2
        pane = MagicMock()
        pane._session_cwd = "C:/work/api"
        pane.session.display_lines.return_value = ["done."]

        path = _write_progress_marker(
            "proj", "backend", ps, pane, status="gave_up", reason="round_cap"
        )

        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "gave_up"
        assert data["reason"] == "round_cap"
        assert data["role"] == "backend"
        assert data["project"] == "proj"
        assert data["task"] == "fix the thing"
        assert data["task_file"] == "/tmp/task.txt"
        assert data["cwd"] == "C:/work/api"
        assert data["output_tail"] == "done."
        assert data["park_rounds"] == 2

    def test_overwrites_on_repeated_calls(self) -> None:
        ps = PaneState()
        ps.last_assigned_task = "task"
        pane = MagicMock()
        pane._session_cwd = None
        pane.session = None

        p1 = _write_progress_marker("proj", "backend", ps, pane, status="parked")
        p2 = _write_progress_marker("proj", "backend", ps, pane, status="resumed")

        assert p1 == p2
        assert json.loads(p2.read_text(encoding="utf-8"))["status"] == "resumed"

    def test_write_failure_returns_none(self, monkeypatch) -> None:
        from pathlib import Path

        ps = PaneState()
        monkeypatch.setattr(
            Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        assert _write_progress_marker("proj", "backend", ps, None, status="parked") is None


class TestGiveUpAutoResume:
    def test_dump_includes_hint_task_preview_and_marker_path(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "implement the auth endpoint"
        pane = MagicMock()
        pane._session_cwd = None
        pane.session.display_lines.return_value = ["ok, done implementing."]
        o._panes_by_project["proj"] = {"backend": pane}

        o._give_up_auto_resume("proj", "backend", ps, reason="round_cap")

        o._notify_lead.assert_called_once()
        msg = o._notify_lead.call_args.args[1]
        assert "งานอาจเสร็จสมบูรณ์แล้ว" in msg  # verify-before-discard hint
        assert "implement the auth endpoint" in msg
        assert "ok, done implementing." in msg
        assert "status dump เต็ม" in msg

    def test_long_task_preview_is_truncated(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "x" * (auto_resume.GIVE_UP_TASK_PREVIEW_CHARS + 50)
        o._give_up_auto_resume("proj", "backend", ps, reason="round_cap")
        msg = o._notify_lead.call_args.args[1]
        assert "x" * auto_resume.GIVE_UP_TASK_PREVIEW_CHARS + "…" in msg
        assert "x" * (auto_resume.GIVE_UP_TASK_PREVIEW_CHARS + 1) not in msg

    def test_writes_progress_marker_to_disk(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        o._give_up_auto_resume("proj", "backend", ps, reason="round_cap")
        path = _progress_marker_path("proj", "backend")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "gave_up"
        assert data["reason"] == "round_cap"

    def test_checks_uncommitted_when_cwd_known(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        pane = MagicMock()
        pane._session_cwd = "C:/work/api"
        pane.session.display_lines.return_value = []
        o._panes_by_project["proj"] = {"backend": pane}
        with patch.object(o, "_check_uncommitted_async") as check:
            o._give_up_auto_resume("proj", "backend", ps, reason="round_cap")
        check.assert_called_once_with("proj", "backend", "C:/work/api")

    def test_skips_uncommitted_check_when_cwd_unknown(self) -> None:
        o = _bare_orch()
        ps = o._ps("proj::backend")
        ps.last_assigned_task = "do the thing"
        # no pane registered for this project/role → _pane_cwd returns None
        with patch.object(o, "_check_uncommitted_async") as check:
            o._give_up_auto_resume("proj", "backend", ps, reason="round_cap")
        check.assert_not_called()
