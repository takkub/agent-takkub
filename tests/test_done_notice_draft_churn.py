"""Repro + regression for #108 — draft-hold spill<->flush churn loop.

Real-world (events.log 2026-07-09 14:04:51-14:06:46): 2 done notices in the
durable queue + a Lead draft read as pending cycled `lead_notify_draft_spill`
x2 + `done_notices_flushed` every ~5s (the reaper tick) for ~2 minutes, until
the #70 stale-force-flush escalation broke the loop.

Root cause: `_reap_pending_done_notices` only gated on `is_at_ready_prompt()`,
not on the Lead draft-hold guard. While a draft was pending it still called
`_flush_pending_done_notices`, which moved every durable item into the live
`_lead_notify_queue` one at a time via `_notify_lead` (arming the pump per
item). `_pump_lead_notify`'s own draft-hold-expired check was already stuck
True (pending_since older than DRAFT_HOLD_TIMEOUT_S, and the draft never
cleared) so each item was spilled straight back to durable synchronously,
producing one `lead_notify_draft_spill` log per item — hence the "x2" — and
leaving the items exactly where they started for the next reaper tick to
repeat the whole cycle.

Fix: `_flush_pending_done_notices` now checks `_lead_can_accept_injection`
before moving anything out of the durable queue (park silently while a draft
blocks).

#118 follow-up: the original fix folded "ready but draft-blocked" into the
same staleness-accumulating branch as "not ready", so the #70 force-flush
safety net would eventually bypass a genuinely-stuck draft too — clobbering
whatever the user was mid-typing. `_reap_pending_done_notices` now keeps the
two conditions separate: only "not ready" (a transient, non-user-caused
state) escalates to force-flush after `_DONE_NOTICE_STALE_S`; "ready but
draft-blocked" (user-caused) parks indefinitely in the durable queue — no
timeout, no force — until the draft clears on its own.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.lead_draft_state import NONEMPTY, LeadDraftState
from agent_takkub.lead_inbox import _DONE_NOTICE_STALE_S
from agent_takkub.orchestrator import Orchestrator

PROJECT = "churnproj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _lead(*, ready: bool = True) -> MagicMock:
    pane = MagicMock()
    s = MagicMock()
    s.is_alive = True
    s.is_at_ready_prompt = MagicMock(return_value=ready)
    s.write = MagicMock()
    pane.session = s
    return pane


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda project: project))
    monkeypatch.setattr(Orchestrator, "_save_pending_done_notices", lambda self, p: None)
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _stuck_draft(now: float, age_s: float) -> LeadDraftState:
    """A draft that has been sitting nonempty for *age_s* seconds and — like
    the real bug — never clears (no Enter/Esc/Ctrl+C/Ctrl+U ever observed)."""
    return LeadDraftState(state=NONEMPTY, draft_len=3, pending_since=now - age_s)


class TestDraftPendingParksInsteadOfChurning:
    def test_reap_does_not_move_items_out_of_durable_while_draft_blocks(
        self, orch: Orchestrator
    ) -> None:
        lead = _lead(ready=True)
        orch._panes_by_project[PROJECT] = {"lead": lead}
        orch._pending_done_notices = {
            PROJECT: [
                {"role": "backend", "note": "n", "body": "DONE-1"},
                {"role": "qa", "note": "n", "body": "DONE-2"},
            ]
        }
        now = 1_000_000.0
        # Draft has been pending 60s — well past the reaper tick cadence but
        # short of the 180s draft-hold timeout, so nothing should spill yet.
        orch._lead_draft_state = {PROJECT: _stuck_draft(now, 60.0)}

        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=now),
        ):
            orch._reap_pending_done_notices()

        lead.session.write.assert_not_called()
        assert len(orch._pending_done_notices.get(PROJECT, [])) == 2, (
            "items must stay parked in durable, not bounce into the live queue"
        )
        assert not getattr(orch, "_lead_notify_queue", {}).get(PROJECT), (
            "flush must not have handed anything to the live pump while draft blocks"
        )

    def test_no_churn_across_repeated_ticks(self, orch: Orchestrator) -> None:
        """The exact #108 shape: durable notices + a pending draft + repeated
        5s reaper ticks must not spill/re-flush every tick."""
        lead = _lead(ready=True)
        orch._panes_by_project[PROJECT] = {"lead": lead}
        orch._pending_done_notices = {
            PROJECT: [
                {"role": "backend", "note": "n", "body": "DONE-1"},
                {"role": "qa", "note": "n", "body": "DONE-2"},
            ]
        }
        now = 1_000_000.0
        orch._lead_draft_state = {PROJECT: _stuck_draft(now, 200.0)}  # past 180s hold timeout

        events: list[str] = []
        with patch(
            "agent_takkub.lead_inbox._log_event", side_effect=lambda name, **kw: events.append(name)
        ):
            for tick in range(6):  # simulate ~30s of 5s ticks
                t = now + tick * 5
                with (
                    patch("agent_takkub.orchestrator.QTimer.singleShot"),
                    patch("agent_takkub.lead_inbox.time.time", return_value=t),
                ):
                    orch._reap_pending_done_notices()

        assert "lead_notify_draft_spill" not in events, (
            "draft-hold spill must not fire from the reaper loop (#108 churn)"
        )
        assert events.count("done_notices_flushed") == 0, (
            "flush must not run at all while draft blocks — no churn"
        )
        lead.session.write.assert_not_called()
        assert len(orch._pending_done_notices.get(PROJECT, [])) == 2

    def test_stale_force_flush_never_fires_for_a_genuinely_stuck_draft(
        self, orch: Orchestrator
    ) -> None:
        """#118: a genuinely-stuck draft must NEVER be force-bypassed, no
        matter how long it's been pending — force-flushing over an
        unsubmitted draft clobbers whatever the user is mid-typing, which is
        worse than a late notice. Only the *not-ready* branch escalates to
        force-flush (see test_perpetually_not_ready_lead_force_flushes below
        / tests/test_reap_multiproject.py)."""
        lead = _lead(ready=True)
        orch._panes_by_project[PROJECT] = {"lead": lead}
        orch._pending_done_notices = {PROJECT: [{"role": "backend", "note": "n", "body": "DONE-1"}]}
        now = 1_000_000.0
        orch._lead_draft_state = {PROJECT: _stuck_draft(now, 200.0)}

        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=now),
        ):
            orch._reap_pending_done_notices()
        assert "DONE-1" not in "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        # Draft-blocked never arms the staleness clock — it isn't the
        # escalation path.
        assert PROJECT not in orch._pending_done_since

        later = now + _DONE_NOTICE_STALE_S + 1
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=later),
        ):
            orch._reap_pending_done_notices()

        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "DONE-1" not in written, "draft-hold must never be force-bypassed (#118)"
        assert orch._pending_done_notices.get(PROJECT), "notice must stay parked, not dropped"
        assert PROJECT not in orch._pending_done_since

    def test_draft_clears_then_flushes_normally(self, orch: Orchestrator) -> None:
        """A parked notice must deliver as soon as the draft clears — parking
        is not a dead end."""
        lead = _lead(ready=True)
        orch._panes_by_project[PROJECT] = {"lead": lead}
        orch._pending_done_notices = {PROJECT: [{"role": "backend", "note": "n", "body": "DONE-1"}]}
        now = 1_000_000.0
        orch._lead_draft_state = {PROJECT: _stuck_draft(now, 500.0)}

        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=now),
        ):
            orch._reap_pending_done_notices()
        assert orch._pending_done_notices.get(PROJECT), "still parked while draft blocks"

        # Draft clears (submitted or deleted).
        from agent_takkub.lead_draft_state import LeadDraftState

        orch._lead_draft_state = {PROJECT: LeadDraftState()}
        later = now + 1
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=later),
        ):
            orch._reap_pending_done_notices()

        assert not orch._pending_done_notices.get(PROJECT), "must flush once draft clears"

    def test_not_ready_still_force_flushes_even_with_stale_draft_state(
        self, orch: Orchestrator
    ) -> None:
        """The not-ready escalation path is independent of draft state — a
        stale/irrelevant draft record left over from an earlier ready window
        must not block the #70 not-ready safety net."""
        lead = _lead(ready=False)
        orch._panes_by_project[PROJECT] = {"lead": lead}
        orch._pending_done_notices = {PROJECT: [{"role": "backend", "note": "n", "body": "DONE-1"}]}
        now = 1_000_000.0
        orch._lead_draft_state = {PROJECT: _stuck_draft(now, 200.0)}

        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=now),
        ):
            orch._reap_pending_done_notices()

        later = now + _DONE_NOTICE_STALE_S + 1
        with (
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.lead_inbox.time.time", return_value=later),
        ):
            orch._reap_pending_done_notices()

        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "DONE-1" in written, "not-ready escalation must still fire"
        assert not orch._pending_done_notices.get(PROJECT)
