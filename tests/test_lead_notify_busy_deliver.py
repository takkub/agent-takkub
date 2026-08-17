"""Targeted tests for #279 — Lead notices must not wait ~90s for an idle Lead.

Production evidence (events.log, 2026-08-16, saas_admin, one working day):
36 `done` calls against 52 `lead_notify_spill` + 32 `done_notice_force_flush`.
Virtually every teammate report took the full escalation chain — 75 busy
retries (~30s) → spill to the durable store → reaper staleness window (60s) →
`_force_deliver_done_notices` pastes it anyway — because an autonomous Lead is
busy nearly all the time and `is_at_ready_prompt()` mostly never comes true on
its own. Worse, `takkub wait` stays pending until the report has left the
pipeline (#163), so the wait Lead is blocked in is itself what keeps Lead busy.

Two changes, tested here:
  1. `_pump_lead_notify` prefers an idle Lead for `_LEAD_BUSY_DELIVER_AFTER_S`
     and then delivers into the busy one anyway — except while Lead sits on a
     trust/permission/tty prompt, where a paste answers the modal instead of
     reaching the composer.
  2. `_notify_lead` skips the digest debounce entirely for a role a `takkub
     wait` registration is currently blocked on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.lead_inbox import _LEAD_BUSY_DELIVER_AFTER_S
from agent_takkub.orchestrator import Orchestrator

PROJECT = "busydeliver"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session(ready: bool = False) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock(return_value=True)
    s.is_at_ready_prompt.return_value = ready
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    return s


def _pane(state: str = "working", session=None) -> MagicMock:
    p = MagicMock()
    p.state = state
    p.session = session
    return p


def _written(session: MagicMock) -> str:
    parts: list[str] = []
    for call in session.write.call_args_list:
        value = call.args[0]
        parts.append(value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value))
    return "".join(parts)


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    o._active_waits = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestBusyLeadStillGetsDelivery:
    def test_busy_lead_receives_notice_after_prefer_idle_window(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core #279 fix: a Lead that never reaches its ready prompt (the
        normal state of an autonomous Lead mid-turn / blocked in `takkub
        wait`) still gets the notice — without the spill → reaper → 60s
        force-flush detour."""
        clock = [1_000_000.0]
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: clock[0])
        lead = _pane(session=_live_session(ready=False))
        orch._panes_by_project[PROJECT] = {"lead": lead}

        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] build broke")
            assert not lead.session.write.called, "must prefer an idle Lead first"

            # Still inside the prefer-idle window — nothing yet.
            clock[0] += _LEAD_BUSY_DELIVER_AFTER_S - 0.5
            orch._pump_lead_notify(PROJECT)
            assert not lead.session.write.called

            # Past it: deliver into the busy Lead rather than spilling.
            clock[0] += 1.0
            orch._pump_lead_notify(PROJECT)

        assert "[backend FAILED] build broke" in _written(lead.session)
        durable = getattr(orch, "_pending_done_notices", {})
        assert not durable.get(PROJECT), "must not spill to durable"

    def test_rest_of_the_burst_does_not_re_pay_the_window(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four notices behind one busy Lead must cost one prefer-idle window,
        not four."""
        clock = [1_000_000.0]
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: clock[0])
        lead = _pane(session=_live_session(ready=False))
        orch._panes_by_project[PROJECT] = {"lead": lead}

        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] one")
            orch._notify_lead(PROJECT, "[qa FAILED] two")
            clock[0] += _LEAD_BUSY_DELIVER_AFTER_S + 1
            orch._pump_lead_notify(PROJECT)
            # Second item, same drain, no further wall-clock advance.
            orch._lead_notify_verify_active.discard(PROJECT)
            orch._pump_lead_notify(PROJECT)

        written = _written(lead.session)
        assert "one" in written and "two" in written
        assert PROJECT not in orch._lead_notify_busy_since, "clock must clear once drained"

    def test_prompt_blocked_lead_never_gets_a_forced_paste(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trust/permission modal swallows keystrokes as its own answer —
        the one busy state where waiting (and eventually spilling) is still
        strictly correct."""
        clock = [1_000_000.0]
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: clock[0])
        session = _live_session(ready=False)
        session.is_at_trust_prompt.return_value = True
        lead = _pane(session=session)
        orch._panes_by_project[PROJECT] = {"lead": lead}

        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] build broke")
            clock[0] += _LEAD_BUSY_DELIVER_AFTER_S * 10
            orch._pump_lead_notify(PROJECT)

        assert "[backend FAILED]" not in _written(lead.session)

    def test_draft_hold_still_wins_over_the_busy_escalation(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#3's guard is downstream of the ready gate and must keep holding:
        the escalation may only skip waiting for IDLE, never the user's own
        unsubmitted draft."""
        clock = [1_000_000.0]
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: clock[0])
        lead = _pane(session=_live_session(ready=False))
        orch._panes_by_project[PROJECT] = {"lead": lead}
        monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda p: False)
        monkeypatch.setattr(orch, "_lead_draft_hold_expired", lambda p: False)

        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] build broke")
            clock[0] += _LEAD_BUSY_DELIVER_AFTER_S * 10
            orch._pump_lead_notify(PROJECT)

        assert "[backend FAILED]" not in _written(lead.session)

    def test_backoff_clock_resets_between_bursts(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale `_lead_notify_busy_since` left behind by a delivered burst
        would make the NEXT one escalate on its very first poll, skipping the
        prefer-idle window entirely."""
        clock = [1_000_000.0]
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: clock[0])
        lead = _pane(session=_live_session(ready=False))
        orch._panes_by_project[PROJECT] = {"lead": lead}

        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] first")
            clock[0] += _LEAD_BUSY_DELIVER_AFTER_S + 1
            orch._pump_lead_notify(PROJECT)
            assert "first" in _written(lead.session)
            assert PROJECT not in orch._lead_notify_busy_since, "backoff clock left armed"

            # Second burst, much later. (Clear the in-flight submit-verify
            # marker the delivered item registered — its on_settled rides a
            # QTimer this test has patched out.)
            orch._lead_notify_verify_active.discard(PROJECT)
            clock[0] += 600.0
            orch._notify_lead(PROJECT, "[qa FAILED] second")

        # Prefer-idle window starts over from now, so nothing is delivered yet.
        assert "second" not in _written(lead.session)
        assert orch._lead_notify_busy_since.get(PROJECT) == clock[0]


class TestWaitedOnRoleSkipsDigestDebounce:
    @pytest.fixture(autouse=True)
    def _real_digest_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # conftest pins the window to 0 (legacy immediate delivery) for the
        # suite at large; these tests are specifically about the debounce.
        monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "60000")

    def _busy_lead(self, orch: Orchestrator) -> MagicMock:
        lead = _pane(session=_live_session(ready=True))
        orch._panes_by_project[PROJECT] = {"lead": lead, "backend": _pane("working")}
        return lead

    def test_report_for_a_watched_role_flushes_immediately(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lead = self._busy_lead(orch)
        orch._active_waits[PROJECT] = {
            "wait_id": "abc",
            "roles": ["backend"],
            "started_ts": 0.0,
            "timeout_s": 600.0,
            "last_poll_ts": 0.0,
        }
        monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda p: True)
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend done] shipped", from_role="backend")

        assert "shipped" in _written(lead.session), "a watched role must skip the debounce"

    def test_report_for_an_unwatched_role_still_debounces(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lead = self._busy_lead(orch)
        orch._active_waits[PROJECT] = {
            "wait_id": "abc",
            "roles": ["qa"],
            "started_ts": 0.0,
            "timeout_s": 600.0,
            "last_poll_ts": 0.0,
        }
        monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda p: True)
        monkeypatch.setattr(orch, "_other_roles_still_active", lambda p, r: True)
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend done] shipped", from_role="backend")

        assert "shipped" not in _written(lead.session)
        assert orch._lead_digest_queue[PROJECT], "must still be waiting in the digest window"

    def test_shard_report_matches_a_wait_on_the_base_role(self, orch: Orchestrator) -> None:
        orch._active_waits[PROJECT] = {
            "wait_id": "abc",
            "roles": ["backend"],
            "started_ts": 0.0,
            "timeout_s": 600.0,
            "last_poll_ts": 0.0,
        }
        assert orch._wait_is_watching_role(PROJECT, "backend#2")
        assert not orch._wait_is_watching_role(PROJECT, "frontend")
        assert not orch._wait_is_watching_role(PROJECT, None)
