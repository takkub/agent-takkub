"""Tests for the trust/tty-prompt-blocked delivery notice (issue #186).

A worktree spawn of a gemini-family (agy) pane hung on the folder-trust
modal. `_send_when_ready`'s busy-wait couldn't tell "genuinely producing
work output" apart from "sitting on a modal that periodically redraws" —
both advance `seconds_since_output()` — so the Lead heard nothing concrete
until either the modal cleared on its own or the BUSY_WAIT_CEILING_SEC
absolute ceiling fired (up to 30 minutes later). Worse, the eventual
best-effort blind paste at the hard timeout could land on the modal itself
and lose the task outright (a stricter loss than an ordinary unconfirmed
paste).

`_prompt_block_reason()` / `_warn_lead_delivery_blocked_prompt()` recognise
the blocked-on-prompt state on the very first poll (independent of elapsed
time) and warn the Lead immediately; `_deliver()` refuses to blind-paste
while still on a recognised prompt, deferring instead for a bounded grace.
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
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


def _ready_after(n: int):
    """side_effect callable: False for the first *n* calls, True forever
    after — avoids StopIteration against the 33-consecutive-poll ready
    streak _send_when_ready requires before it trusts is_at_ready_prompt()."""
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] > n

    return _fn


def _blocked_then_clear(n: int):
    """side_effect callable: True (blocked) for the first *n* calls, False
    (clear) forever after."""
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] <= n

    return _fn


def _string_then_none(n: int, s: str):
    """Like `_blocked_then_clear`, but for a marker predicate that returns a
    string (`is_blocked_on_tty_prompt`) rather than a bare bool."""
    calls = {"n": 0}

    def _fn() -> str | None:
        calls["n"] += 1
        return s if calls["n"] <= n else None

    return _fn


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


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


class TestPromptBlockedEarlyNotice:
    def test_trust_prompt_warns_lead_on_first_poll_not_after_timeout(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The warning must fire immediately on detection, well before the
        pane's own delivery hard timeout, unlike busy-wait/unconfirmed."""
        lead = _pane(_live_session())
        gemini = _pane(_live_session())
        # Stays blocked for several polls, then clears and delivers — large
        # max_wait_ms so the busy/stall hard-timeout branch never runs; the
        # early notice must fire anyway, well before that. (#376 round 2:
        # the ready-streak gate now also holds at 0 for as long as this
        # stays True — a modal that never cleared at all would mean
        # delivery legitimately never happens via the ready path, which is
        # the fix, but is not what THIS test is about.)
        gemini.session.is_at_trust_prompt.side_effect = _blocked_then_clear(5)
        gemini.session.is_at_ready_prompt.side_effect = _ready_after(3)
        gemini.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        blocked = [m for m in warnings if "[delivery-blocked-prompt]" in m]
        assert len(blocked) == 1
        assert "gemini" in blocked[0]
        assert "#186" in blocked[0]
        # No busy-wait/unconfirmed noise for a pane that recovers normally.
        assert not any("[delivery-busy-wait]" in m for m in warnings)

    def test_tty_prompt_also_recognised(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_trust_prompt.return_value = False
        backend.session.is_blocked_on_tty_prompt.side_effect = _string_then_none(
            5, "Overwrite? (y/n)"
        )
        backend.session.is_at_ready_prompt.side_effect = _ready_after(3)
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        blocked = [m for m in _written_strings(lead.session) if "[delivery-blocked-prompt]" in m]
        assert len(blocked) == 1

    def test_warns_only_once_across_many_polls(self, orch: Orchestrator, monkeypatch) -> None:
        """Detection is checked every poll (the check is cheap and the pane
        could still be blocked when it first runs) but the Lead notice is
        gated on a one-shot flag — a pane blocked the whole way through must
        still only produce a single warning, not one per poll."""
        lead = _pane(_live_session())
        gemini = _pane(_live_session())
        # Blocked for many polls (well past the one-shot warning firing on
        # the first) before it finally clears — bounded so delivery still
        # completes and this test doesn't recurse anywhere near the real
        # (100_000ms / 150ms) poll budget.
        gemini.session.is_at_trust_prompt.side_effect = _blocked_then_clear(20)
        gemini.session.is_at_ready_prompt.side_effect = _ready_after(4)
        gemini.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        blocked = [m for m in _written_strings(lead.session) if "[delivery-blocked-prompt]" in m]
        assert len(blocked) == 1

    def test_no_warning_when_never_blocked(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")

        assert not any("[delivery-blocked-prompt]" in m for m in _written_strings(lead.session))


class TestWarnLeadBlockedPromptDirect:
    def test_message_contains_marker_role_and_issue_ref(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_prompt("gemini", "P", "trust")
        msg = lead.session.write.call_args[0][0]
        assert "[delivery-blocked-prompt]" in msg
        assert "gemini" in msg
        assert "#186" in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_prompt("lead", "P", "trust")
        lead.session.write.assert_not_called()

    def test_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"backend": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_prompt("backend", "P", "trust")  # must not raise


class TestWarnLeadBlockedCeilingDirect:
    def test_message_contains_marker_role_and_issue_ref(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_ceiling("gemini", "P", "trust")
        msg = lead.session.write.call_args[0][0]
        assert "[delivery-blocked-ceiling]" in msg
        assert "gemini" in msg
        assert "#484" in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_ceiling("lead", "P", "trust")
        lead.session.write.assert_not_called()

    def test_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"backend": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_ceiling("backend", "P", "trust")  # must not raise


class TestNeverBlindPasteIntoModal:
    """The hard-timeout best-effort paste must not fire while the pane is
    still visibly on a trust/tty prompt — that paste would land as
    keystrokes on the modal, losing the task outright rather than merely
    leaving it unconfirmed."""

    def test_gives_up_without_pasting_once_still_on_trust_prompt_past_ceiling(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#484: the OLD contract ("still eventually pastes, last resort")
        was itself the bug — a live incident (2026-09-04) showed those bytes
        land as keystrokes on the trust modal (observed selecting "No,
        exit" and killing the pane), then a delivery that can never confirm
        loops through `_reap_stale_deliveries`'s stale-reap notice every TTL
        forever. A confirmed prompt block must never be pasted into, however
        long it has been deferred — the fix gives up cleanly instead."""
        import agent_takkub.lead_inbox as li

        # Small defer ceiling — fake clock, same technique the existing
        # BUSY_WAIT_CEILING_SEC tests use — so the synchronous singleShot
        # stub (each tick recurses instead of returning to an event loop)
        # doesn't blow the Python recursion limit proving the same behavior
        # a real 30s/150ms-tick ceiling would.
        monkeypatch.setattr(li, "_PROMPT_BLOCK_DEFER_CEILING_MS", 300)
        lead = _pane(_live_session())
        gemini = _pane(_live_session())
        gemini.session.is_at_trust_prompt.return_value = True  # never clears
        gemini.session.is_at_ready_prompt.return_value = False
        gemini.session.seconds_since_output.return_value = float("inf")  # "stuck" path
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=300, project="P")

        # is_at_trust_prompt() returning True forever is the pathological
        # case: the deferral ceiling (30s @ 150ms polls) is eventually
        # reached and the fix gives up deferring — proven by the dedicated
        # log event.
        assert any(
            c.args and c.args[0] == "task_deliver_prompt_defer_ceiling" for c in log.call_args_list
        )
        # Never pastes into the still-confirmed modal — no bytes land on it.
        assert not gemini.session.write.called
        # The Lead hears a distinct, accurate "gave up, never delivered"
        # notice — not the ordinary [delivery-unconfirmed] wording, which
        # would misleadingly imply a paste WAS attempted.
        warnings = _written_strings(lead.session)
        blocked_ceiling = [m for m in warnings if "[delivery-blocked-ceiling]" in m]
        assert len(blocked_ceiling) == 1
        assert "gemini" in blocked_ceiling[0]
        assert "#484" in blocked_ceiling[0]
        assert not any("[delivery-unconfirmed]" in m for m in warnings)

    def test_delivers_normally_once_prompt_clears_before_defer_ceiling(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The common case: _auto_trust (or a human) clears the modal well
        inside the defer ceiling — delivery proceeds via the normal
        ready-prompt path, not a blind paste at all."""
        lead = _pane(_live_session())
        gemini = _pane(_live_session())
        # Blocked on trust for the first few polls, then clears.
        gemini.session.is_at_trust_prompt.side_effect = _blocked_then_clear(3)
        gemini.session.is_at_ready_prompt.side_effect = _ready_after(3)
        gemini.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        assert gemini.session.write.called
        warnings = _written_strings(lead.session)
        assert not any("[delivery-unconfirmed]" in m for m in warnings)

    def test_prompt_block_reason_helper_distinguishes_trust_vs_tty(self) -> None:
        from agent_takkub.lead_inbox import _prompt_block_reason

        s = MagicMock()
        s.is_at_trust_prompt.return_value = True
        s.is_blocked_on_permission_prompt.return_value = None
        s.is_blocked_on_tty_prompt.return_value = None
        assert _prompt_block_reason(s) == "trust"

        s2 = MagicMock()
        s2.is_at_trust_prompt.return_value = False
        s2.is_blocked_on_permission_prompt.return_value = None
        s2.is_blocked_on_tty_prompt.return_value = "Overwrite? (y/n)"
        assert _prompt_block_reason(s2) == "tty"

        s3 = MagicMock()
        s3.is_at_trust_prompt.return_value = False
        s3.is_blocked_on_permission_prompt.return_value = None
        s3.is_blocked_on_tty_prompt.return_value = None
        assert _prompt_block_reason(s3) is None

        s4 = MagicMock()
        s4.is_at_trust_prompt.return_value = False
        s4.is_blocked_on_permission_prompt.return_value = "1. Yes"
        assert _prompt_block_reason(s4) == "permission"
