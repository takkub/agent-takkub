"""Targeted tests for #266: notices must not be presented to Lead as
current when the condition they describe has already resolved by the time
they're actually flushed.

Real evidence from a single session: a `[delivery-unconfirmed] codex ...`
notice arrived at Lead's pane AFTER that codex pane had already finished
its task and closed; a worktree-uncommitted warning arrived after Lead had
already committed and merged the very worktree it was warning about. The
enqueue-time condition was never re-checked at flush time, and most system
warnings carried no occurrence timestamp at all — so Lead read stale news
as the present.

Two independent mechanisms, both applied right before a notice actually
lands (`_pump_lead_notify` for the live queue, `_force_deliver_done_notices`
for the durable-force path):

  1. `_revalidate_system_notice` — the 5 delivery-health system markers
     ([delivery-unconfirmed]/[spawn-stuck]/[delivery-boot-stall]/
     [spawn-failed]/[delivery-busy-wait]) are re-checked against the target
     role's CURRENT pane state; a role whose pane has since reached a
     terminal state (or vanished entirely) gets the body prefixed with a
     "resolved since" banner instead of presenting the claim as current.
  2. `_occurred_stamp` — every notice (not just the pre-existing done-digest
     treatment) is now prefixed with when it actually occurred, surviving
     durable round-trips via `queued_ts` threaded through `_notify_lead`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.lead_inbox import _occurred_stamp
from agent_takkub.orchestrator import Orchestrator

PROJECT = "revalidate-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    s.is_at_ready_prompt.return_value = True
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
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def test_occurred_stamp_renders_clock_and_age() -> None:
    now = 1_000_000.0
    stamp = _occurred_stamp(now - 5.0, now)
    assert stamp.startswith("[")
    assert "5s ago" in stamp
    assert stamp.endswith("] ")


def test_occurred_stamp_empty_when_unknown() -> None:
    assert _occurred_stamp(None) == ""


class TestSystemNoticeRevalidatedAtFlush:
    def test_delivery_unconfirmed_flagged_resolved_when_pane_already_closed(
        self, orch: Orchestrator
    ) -> None:
        lead = _pane(session=_live_session())
        # Lead starts BUSY so the enqueue doesn't deliver synchronously —
        # lets this test mutate pane state in between enqueue and the
        # eventual flush, reproducing the actual #266 race (enqueue-time
        # truth ≠ flush-time truth) instead of just testing the revalidate
        # helper's logic in isolation.
        lead.session.is_at_ready_prompt.return_value = False
        orch._panes_by_project[PROJECT] = {"lead": lead, "codex": _pane("working")}
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(
                PROJECT,
                "⚠️ [delivery-unconfirmed] codex pane ไม่ถึง ready prompt ใน 90s",
            )
            assert not lead.session.write.called  # confirms it queued, didn't deliver

            # codex finished and closed before this notice ever reached Lead.
            del orch._panes_by_project[PROJECT]["codex"]
            lead.session.is_at_ready_prompt.return_value = True
            orch._pump_lead_notify(PROJECT)

        written = _written(lead.session)
        assert "คลี่คลายแล้ว" in written
        assert "delivery-unconfirmed] codex pane ไม่ถึง ready prompt" in written

    def test_delivery_unconfirmed_not_flagged_when_pane_still_active(
        self, orch: Orchestrator
    ) -> None:
        lead = _pane(session=_live_session())
        orch._panes_by_project[PROJECT] = {"lead": lead, "codex": _pane("working")}
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(
                PROJECT,
                "⚠️ [delivery-unconfirmed] codex pane ไม่ถึง ready prompt ใน 90s",
            )

        written = _written(lead.session)
        assert "คลี่คลายแล้ว" not in written

    def test_delivery_busy_wait_also_gets_revalidated(self, orch: Orchestrator) -> None:
        """#266's evidence item 3 — busy-wait wasn't even in the marker set
        _system_marker_role recognised before this fix."""
        lead = _pane(session=_live_session())
        lead.session.is_at_ready_prompt.return_value = False
        orch._panes_by_project[PROJECT] = {"lead": lead, "kimi": _pane("working")}
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(
                PROJECT,
                "⏳ [delivery-busy-wait] kimi pane ยังไม่ถึง ready prompt แต่มี output ล่าสุดเมื่อ 3s ที่แล้ว",
            )
            assert not lead.session.write.called

            del orch._panes_by_project[PROJECT]["kimi"]
            lead.session.is_at_ready_prompt.return_value = True
            orch._pump_lead_notify(PROJECT)

        written = _written(lead.session)
        assert "คลี่คลายแล้ว" in written

    def test_plain_done_notice_is_never_revalidated_or_flagged(self, orch: Orchestrator) -> None:
        """Only the 5 delivery-health system markers are re-checkable this
        way — a plain `[role done]`/`[role FAILED]` body must pass through
        untouched regardless of that role's current pane state."""
        lead = _pane(session=_live_session())
        orch._panes_by_project[PROJECT] = {"lead": lead}
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[backend FAILED] build broke")

        written = _written(lead.session)
        assert "คลี่คลายแล้ว" not in written
        assert "[backend FAILED] build broke" in written

    def test_direct_revalidate_helper_unchanged_for_non_marker_body(
        self, orch: Orchestrator
    ) -> None:
        body = "[qa done] all green"
        assert orch._revalidate_system_notice(PROJECT, body) == body


class TestLiveNoticeCarriesOccurredStamp:
    def test_blocking_notice_shows_a_clock_stamp(self, orch: Orchestrator) -> None:
        lead = _pane(session=_live_session())
        orch._panes_by_project[PROJECT] = {"lead": lead}
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._notify_lead(PROJECT, "[qa FAILED] checkout is broken")

        written = _written(lead.session)
        assert "[qa FAILED] checkout is broken" in written
        idx = written.index("[qa FAILED]")
        prefix = written[:idx]
        assert ":" in prefix  # "[HH:MM:SS ..." clock stamp precedes the body


class TestDurableRoundTripPreservesOccurrence:
    def test_flush_pending_done_notices_keeps_original_timestamp(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Lead absent when the notice is first raised — falls into the
        # durable store with its own occurrence timestamp.
        orch._panes_by_project[PROJECT] = {}
        orch._pending_done_notices = {}
        original_ts = 1_000_000.0
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: original_ts)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._notify_lead(PROJECT, "[backend done] shipped while Lead was away")
        assert orch._pending_done_notices[PROJECT][0]["queued_ts"] == original_ts

        # Lead spawns much later — _flush_pending_done_notices requeues the
        # item through _notify_lead. The ORIGINAL timestamp must survive
        # this hop, not reset to "now" (the requeue time).
        later_ts = original_ts + 600.0  # 10 minutes later
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: later_ts)
        lead = _pane(session=_live_session())
        orch._panes_by_project[PROJECT] = {"lead": lead}
        monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda p: True)
        monkeypatch.setattr(orch, "_save_pending_done_notices", lambda p: None)
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._flush_pending_done_notices(PROJECT)

        written = _written(lead.session)
        assert "shipped while Lead was away" in written
        # 10 minutes ago, not "0s ago" — proves the original occurrence
        # time survived the durable→live requeue instead of resetting.
        assert "10m ago" in written


class TestForceDeliverRevalidatesAndStamps:
    """Same two guarantees, exercised through _force_deliver_done_notices
    (the reaper's last-resort path for a Lead that reads perpetually
    not-ready) instead of the normal live-queue pump — that path builds its
    own combined message via `_flagged`, a second call site that needed the
    same fix."""

    def _force_deliver(self, orch: Orchestrator, project_ns: str) -> None:
        with (
            patch("agent_takkub.lead_inbox.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._force_deliver_done_notices(project_ns)

    def test_force_deliver_flags_resolved_delivery_unconfirmed(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch._panes_by_project[PROJECT] = {}
        orch._pending_done_notices = {
            PROJECT: [
                {
                    "role": "system",
                    "note": "notify",
                    "body": "⚠️ [delivery-unconfirmed] codex pane ไม่ถึง ready prompt ใน 90s",
                    "pane_token": None,
                    "queued_ts": 1_000_000.0,
                }
            ]
        }
        monkeypatch.setattr(orch, "_save_pending_done_notices", lambda p: None)
        lead = _pane(session=_live_session())
        # codex's pane is already gone by force-deliver time.
        orch._panes_by_project[PROJECT] = {"lead": lead}

        self._force_deliver(orch, PROJECT)

        written = _written(lead.session)
        assert "คลี่คลายแล้ว" in written

    def test_force_deliver_stamps_occurrence_time(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch._pending_done_notices = {
            PROJECT: [
                {
                    "role": "backend",
                    "note": "notify",
                    "body": "[backend done] shipped",
                    "pane_token": None,
                    "queued_ts": 1_000_000.0,
                }
            ]
        }
        monkeypatch.setattr(orch, "_save_pending_done_notices", lambda p: None)
        monkeypatch.setattr("agent_takkub.lead_inbox.time.time", lambda: 1_000_090.0)
        lead = _pane(session=_live_session())
        orch._panes_by_project[PROJECT] = {"lead": lead}

        self._force_deliver(orch, PROJECT)

        written = _written(lead.session)
        assert "1m ago" in written
