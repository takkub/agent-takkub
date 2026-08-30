"""#440 — a stale draft-hold must not park done digests forever.

Real incident (tunnel, 2026-08-29): two `takkub done` digests spilled to the
durable queue under a draft-hold and stayed there 7h39m with the Lead idle
at an empty prompt, until the user typed. The tracker's "unknown_nonempty"
had no exit other than an explicit submit/cancel byte from the user.
`_lead_can_accept_injection` now resets the state when the screen
contradicts it (claude's composer visibly empty) or when the hold has run
past `DRAFT_HOLD_FORCE_RESET_S` at an idle prompt.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import lead_inbox as li_mod
from agent_takkub.lead_draft_state import (
    DRAFT_HOLD_FORCE_RESET_S,
    EMPTY,
    NONEMPTY,
    UNKNOWN_NONEMPTY,
    LeadDraftState,
)
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _lead(*, alive=True, empty_composer=False, ready=True) -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = alive
    pane.session.is_at_claude_empty_composer.return_value = empty_composer
    pane.session.is_at_ready_prompt.return_value = ready
    return pane


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._lead_draft_state = {}
    o._panes = {}
    monkeypatch.setattr(o, "_project_panes", lambda p=None: o._panes)
    return o


@pytest.fixture
def events(monkeypatch) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(li_mod, "_log_event", lambda ev, **kw: out.append((ev, kw)))
    return out


def _held(state: str, *, since_s_ago: float) -> LeadDraftState:
    return LeadDraftState(state=state, draft_len=3, pending_since=time.time() - since_s_ago)


class TestStructuralReset:
    def test_empty_composer_resets_stale_hold(self, orch, events) -> None:
        orch._panes[LEAD.name] = _lead(empty_composer=True)
        orch._lead_draft_state["P"] = _held(UNKNOWN_NONEMPTY, since_s_ago=20)
        assert orch._lead_can_accept_injection("P") is True
        assert orch._lead_draft_state["P"].state == EMPTY
        assert events and events[0][0] == "lead_draft_state_reset"
        assert events[0][1]["reason"] == "empty_composer"
        assert events[0][1]["prev_state"] == UNKNOWN_NONEMPTY

    def test_real_draft_visible_on_screen_keeps_hold(self, orch, events) -> None:
        # composer NOT empty → the tracker may be right → keep waiting
        orch._panes[LEAD.name] = _lead(empty_composer=False)
        orch._lead_draft_state["P"] = _held(NONEMPTY, since_s_ago=20)
        assert orch._lead_can_accept_injection("P") is False
        assert orch._lead_draft_state["P"].state == NONEMPTY
        assert events == []

    def test_no_lead_pane_keeps_hold(self, orch, events) -> None:
        orch._lead_draft_state["P"] = _held(NONEMPTY, since_s_ago=20)
        assert orch._lead_can_accept_injection("P") is False

    def test_empty_state_short_circuits_without_probe(self, orch) -> None:
        lead = _lead(empty_composer=True)
        orch._panes[LEAD.name] = lead
        orch._lead_draft_state["P"] = LeadDraftState()
        assert orch._lead_can_accept_injection("P") is True
        lead.session.is_at_claude_empty_composer.assert_not_called()


class TestHoldCap:
    def test_hold_past_cap_at_ready_prompt_resets(self, orch, events) -> None:
        # non-claude shape: no structural signal, but idle for > cap
        orch._panes[LEAD.name] = _lead(empty_composer=False, ready=True)
        orch._lead_draft_state["P"] = _held(NONEMPTY, since_s_ago=DRAFT_HOLD_FORCE_RESET_S + 5)
        assert orch._lead_can_accept_injection("P") is True
        assert events[0][1]["reason"] == "hold_cap"
        assert events[0][1]["held_s"] >= DRAFT_HOLD_FORCE_RESET_S

    def test_hold_past_cap_but_lead_busy_keeps_hold(self, orch, events) -> None:
        orch._panes[LEAD.name] = _lead(empty_composer=False, ready=False)
        orch._lead_draft_state["P"] = _held(NONEMPTY, since_s_ago=DRAFT_HOLD_FORCE_RESET_S + 5)
        assert orch._lead_can_accept_injection("P") is False

    def test_hold_under_cap_keeps_hold(self, orch, events) -> None:
        orch._panes[LEAD.name] = _lead(empty_composer=False, ready=True)
        orch._lead_draft_state["P"] = _held(NONEMPTY, since_s_ago=DRAFT_HOLD_FORCE_RESET_S - 60)
        assert orch._lead_can_accept_injection("P") is False

    def test_cap_is_minutes_not_hours(self) -> None:
        assert 10 * 60 <= DRAFT_HOLD_FORCE_RESET_S <= 60 * 60


class TestDurableFlushBenefits:
    def test_flush_pending_done_notices_proceeds_after_reset(
        self, orch, events, monkeypatch
    ) -> None:
        orch._panes[LEAD.name] = _lead(empty_composer=True)
        orch._lead_draft_state["P"] = _held(UNKNOWN_NONEMPTY, since_s_ago=7 * 3600)
        orch._pending_done_notices = {"P": [{"body": "[backend done] x", "queued_ts": 1.0}]}
        monkeypatch.setattr(orch, "_save_pending_done_notices", lambda p: None)
        delivered: list[str] = []
        monkeypatch.setattr(orch, "_notify_lead", lambda p, body, **kw: delivered.append(body))
        orch._flush_pending_done_notices("P")
        assert delivered == ["[backend done] x"]
        assert "P" not in orch._pending_done_notices
