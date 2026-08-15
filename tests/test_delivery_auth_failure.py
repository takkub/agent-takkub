"""Tests for the #248/#247 round-2 follow-up to `_send_when_ready`'s
auth-failure fast-fail path (see lead_inbox.py's `_check` closure).

Round 2 shipped `auth_failure_reason()` wired into `_check` with ZERO
confirmation — a single poll that matched a marker triggered an immediate
`_deliver(unconfirmed=True)`, even when the pane was sitting at its own
ready prompt that same instant (a CLI genuinely stuck on auth can never
reach its ready prompt, so that combination only means the marker text is
stale — e.g. scrolled up from a just-finished test run). This follow-up:

  1. Makes a ready prompt win over a lingering auth marker — no blind
     delivery while the pane is demonstrably idle and reachable normally.
  2. Requires `_AUTH_FAILURE_CONFIRM_POLLS` CONSECUTIVE matches (not one)
     before convicting, filtering a one-frame render artifact.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_inbox import _AUTH_FAILURE_CONFIRM_POLLS
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
    p.model = MagicMock(provider_name="claude")
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


def _ready_only_at(n: int):
    """side_effect callable: True only on call *n* (1-indexed), False on
    every other call including all calls after — infinite (never raises
    StopIteration), unlike a finite side_effect list, so it survives
    however many extra is_at_ready_prompt() polls a post-delivery submit
    verify loop makes."""
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] == n

    return _fn


def _marker_for(n: int, marker: str):
    """side_effect callable: *marker* for the first *n* calls, None forever
    after."""
    calls = {"n": 0}

    def _fn(_provider: str):
        calls["n"] += 1
        return marker if calls["n"] <= n else None

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


class TestReadyPromptWinsOverStaleAuthMarker:
    def test_ready_prompt_delivers_normally_despite_marker_every_poll(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        # A marker that would instant-fail if trusted blindly — but the pane
        # is ALSO at its own ready prompt on every poll, which is proof the
        # CLI is not actually stuck (a genuinely auth-failed CLI never
        # reaches its ready prompt).
        backend.session.is_at_ready_prompt.return_value = True
        backend.session.auth_failure_reason.return_value = "not signed in"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[auth-failure]" in m for m in warnings)
        # Delivered via the normal ready path instead of a blind paste.
        assert backend.session.write.called

    def test_ready_prompt_resets_streak_so_a_later_relapse_needs_its_own_confirmation(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """Build the streak to one short of firing, let a single ready poll
        interrupt it (must reset to 0), then resume the marker — proves the
        eventual fire needs a FULL fresh `_AUTH_FAILURE_CONFIRM_POLLS`-long
        streak counted from AFTER the reset, not the leftover `n_before`
        credit plus whatever is needed to top it up."""
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        n_before = _AUTH_FAILURE_CONFIRM_POLLS - 1
        backend.session.is_at_ready_prompt.side_effect = _ready_only_at(n_before + 1)
        backend.session.auth_failure_reason.return_value = "not signed in"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        auth_warnings = [m for m in warnings if "[auth-failure]" in m]
        assert len(auth_warnings) == 1
        # auth_failure_reason is only ever called on a NOT-ready poll (the
        # ready poll skips it outright) — if the reset had not happened, the
        # leftover n_before credit would let it fire after just ONE more
        # matching poll post-reset (call_count == n_before + 1); a correct
        # reset instead needs a full fresh streak (call_count == n_before +
        # _AUTH_FAILURE_CONFIRM_POLLS).
        assert (
            backend.session.auth_failure_reason.call_count == n_before + _AUTH_FAILURE_CONFIRM_POLLS
        )


class TestAuthFailureRequiresConsecutivePolls:
    def test_single_poll_match_never_fires(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        assert _AUTH_FAILURE_CONFIRM_POLLS > 1
        n = _AUTH_FAILURE_CONFIRM_POLLS - 1
        backend.session.auth_failure_reason.side_effect = _marker_for(n, "not signed in")
        # Not-ready throughout the marker window, then ready — bounds the
        # test instead of riding the busy-wait ceiling out.
        backend.session.is_at_ready_prompt.side_effect = _ready_after(n)
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[auth-failure]" in m for m in warnings)
        assert backend.session.write.called  # delivered normally once ready

    def test_confirm_poll_streak_fires_exactly_once(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = False
        backend.session.auth_failure_reason.return_value = "not signed in"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        auth_warnings = [m for m in warnings if "[auth-failure]" in m]
        assert len(auth_warnings) == 1
        assert "backend" in auth_warnings[0]
        assert "not signed in" in auth_warnings[0]
        assert backend.session.auth_failure_reason.call_count == _AUTH_FAILURE_CONFIRM_POLLS
        assert any(c.args and c.args[0] == "task_deliver_auth_failure" for c in log.call_args_list)
        # Blind-delivered (unconfirmed) — the payload still lands.
        assert backend.session.write.called
