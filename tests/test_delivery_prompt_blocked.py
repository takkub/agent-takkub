"""Tests for the #376 round-2 fix: a trust/permission/tty prompt must hold
the ready-streak at 0, not just trigger a one-shot Lead warning.

`test_delivery_blocked_prompt.py` already covers `_prompt_block_reason()`
itself and the EARLY WARNING firing on the first blocked poll. That warning
used to be the only effect: `can_accept_input()` (delivery_readiness.py)
folded in `account_pending` but not `prompt_blocked`, so once
`is_at_ready_prompt()` read True — which a modal drawn over an idle-looking
footer can do — the ready-streak kept advancing regardless, and `_deliver()`
eventually pasted the task's bytes onto the modal as keystrokes via the
NORMAL ready path (not the blind-paste timeout branch, the only one
`_deliver()` itself already guarded against this). A real worktree spawn hit
exactly this: `task_deliver_blocked_on_prompt` logged at 09:46:25, then
`task_deliver` (submit) at 09:46:26, `accepted` at 09:46:28 — all while the
modal was still on screen.

This file covers the two places that needed the live prompt-block read
folded in, mirroring test_delivery_account_pending.py's
`TestUngatedMarkerHoldsTheReadyStreak` shape for the sibling
`account_pending` signal:

  1. The ready-streak gate in `_send_when_ready`'s poll loop
     (`can_accept_input(..., prompt_blocked=...)`).
  2. `_on_settled`'s belt-and-suspenders re-check right before a delivery is
     marked ACCEPTED (defence in depth for a modal that appears mid-verify,
     after the poll loop already let the write through).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


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
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    s.shows_account_pending_marker.return_value = False
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


def _true_then_false(n: int):
    """side_effect callable: True (blocked) for the first *n* calls, False
    (clear) forever after — for a no-arg boolean predicate like
    `is_at_trust_prompt`."""
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] <= n

    return _fn


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestUngatedPromptBlockHoldsTheReadyStreak:
    """`_prompt_block_reason()` — like `shows_account_pending_marker` — must
    hold the ready-streak at 0 on its own, independent of the one-shot
    warning flag. Reproduces the incident shape: `is_at_ready_prompt()`
    pinned True (a modal drawn over a footer that still reads idle) while
    the trust prompt is still genuinely up — nothing but the round-2 fix
    could be holding delivery back here."""

    def test_ready_streak_never_advances_while_trust_modal_shows(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        gemini = _pane(_live_session())
        gemini.session.is_at_ready_prompt.return_value = True
        gemini.session.is_at_trust_prompt.side_effect = _true_then_false(10)
        gemini.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        assert gemini.session.write.called
        # Delivery can only happen once 7 CONSECUTIVE clear polls piled up
        # AFTER the modal's own 10-poll block (required_polls for
        # max_wait_ms=100_000 is 7) — i.e. no sooner than poll 17 — proving
        # the gate, not luck, held the streak at 0 the whole time the modal
        # showed. Pre-fix code ignored the live prompt-block read entirely
        # once the one-shot warning had already fired and could deliver as
        # early as poll 7.
        assert gemini.session.is_at_trust_prompt.call_count >= 17

    def test_no_modal_case_is_unaffected(self, orch: Orchestrator, monkeypatch) -> None:
        """A pane that never shows any prompt behaves exactly as before the
        round-2 fix — delivers as soon as it reads ready."""
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=1000, project="P")

        assert backend.session.write.called
        assert not any("[delivery-blocked-prompt]" in m for m in _written_strings(lead.session))


class TestSettledStillOnPromptOverridesAccepted:
    """(#376 round 2) `_on_settled` belt-and-suspenders check — same shape
    as the existing `shows_account_pending_marker` re-check it sits next to.
    A not-ready read at settle time is not on its own proof the task was
    accepted: it is exactly what a pane still frozen on a trust/tty/
    permission modal (drawn over the composer) can ALSO look like."""

    def test_settle_still_on_trust_prompt_marks_uncertain_not_accepted(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        reviewer = _pane(_live_session())
        # Ready gate delivers promptly.
        reviewer.session.is_at_ready_prompt.return_value = True
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
        ):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")
            on_settled = verified.call_args.kwargs["on_settled"]
            # Screen at settle time: no longer at the ready prompt (looks
            # submitted) but a trust modal has appeared and is still up.
            reviewer.session.is_at_ready_prompt.return_value = False
            reviewer.session.is_at_trust_prompt.return_value = True
            on_settled()

        delivery = next(iter(orch._delivery_manager._deliveries.values()))
        assert delivery.state.value == "uncertain"
        assert any("[delivery-uncertain]" in m for m in _written_strings(lead.session))

    def test_settle_clears_normally_once_prompt_is_gone(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """Control case: nothing blocking at settle time — behaves exactly
        as before the round-2 fix."""
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = True
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
        ):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")
            on_settled = verified.call_args.kwargs["on_settled"]
            reviewer.session.is_at_ready_prompt.return_value = False
            reviewer.session.is_at_trust_prompt.return_value = False
            on_settled()

        delivery = next(iter(orch._delivery_manager._deliveries.values()))
        assert delivery.state.value == "accepted"
