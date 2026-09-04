"""Tests for the #346 account-pending fast-fail path in `_send_when_ready`'s
`_check` closure (lead_inbox.py) — mirrors test_delivery_auth_failure.py's
shape for the sibling `auth_failure_reason` tier, but for
`account_pending_reason`: a provider-side account/eligibility gate (e.g.
gemini/agy's "Verifying your account...") that is NOT a login/credentials
problem, so it must:

  1. Fire fast (confirmed over `_AUTH_FAILURE_CONFIRM_POLLS` consecutive
     polls), same as auth-failure, instead of riding out the busy/stall path.
  2. Never blind-paste into the gated pane — routes straight to
     `_recover_account_pending_pane` instead.
  3. Warn Lead with wording that does NOT say "log back in" (there is
     nothing to log into).
  4. (#363 regression fix, was previously "let a ready prompt win over a
     lingering marker") NOT let a stale/misread `is_at_ready_prompt()` verdict
     suppress a genuine, persistent `account_pending_reason()` match — the
     original #346 "ready wins" gate assumed `is_at_ready_prompt()` (tight
     6-row `_ready_region`) was trustworthy proof the CLI had cleared its own
     gate, but the real gemini/agy banner plus a realistic footer pushes it
     out of that window while enough footer chrome remains for
     `is_at_ready_prompt()` to misread READY — which used to reset this
     streak to 0 every poll and let the task ride the ordinary ready path
     down to a silent, blind-pasted loss (issue #363). Since
     `account_pending_reason()` now scans its own wider window
     (`_BOOT_MARKER_TAIL_ROWS`, pty_session.py) and is grace-gated on
     `seconds_since_output()`, it is checked unconditionally every poll.
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
    # account_pending_reason is checked ahead of auth_failure_reason in
    # _check — pin both to their non-blocked defaults so a bare MagicMock
    # (truthy) doesn't accidentally convict every test in this module.
    s.auth_failure_reason.return_value = None
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model = MagicMock(provider_name="gemini")
    return p


def _ready_after(n: int):
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] > n

    return _fn


def _ready_only_at(n: int):
    calls = {"n": 0}

    def _fn() -> bool:
        calls["n"] += 1
        return calls["n"] == n

    return _fn


def _marker_for(n: int, marker: str):
    calls = {"n": 0}

    def _fn(_provider: str):
        calls["n"] += 1
        return marker if calls["n"] <= n else None

    return _fn


def _true_then_false(n: int):
    """side_effect callable: True for the first *n* calls, False forever
    after — for boolean marker predicates (shows_account_pending_marker)."""
    calls = {"n": 0}

    def _fn(_provider: str) -> bool:
        calls["n"] += 1
        return calls["n"] <= n

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


class TestAccountPendingIgnoresStaleReadyFlicker:
    """#363 regression: a stale/misread `is_at_ready_prompt()` must not
    suppress a persistent `account_pending_reason()` match — see the module
    docstring's point 4."""

    def test_ready_prompt_true_every_poll_does_not_suppress_confirmation(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = True
        backend.session.account_pending_reason.return_value = "verifying your account"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch.object(orch, "_recover_broken_pane") as recover_broken,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        account_warnings = [m for m in warnings if "[account-pending]" in m]
        assert len(account_warnings) == 1
        recover_broken.assert_called_once()
        # Never blind-pastes, even though is_at_ready_prompt() claimed ready
        # on every single poll — exactly the #363 misread scenario.
        assert not backend.session.write.called

    def test_single_ready_flicker_mid_streak_does_not_reset_it(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        n_before = _AUTH_FAILURE_CONFIRM_POLLS - 1
        backend.session.is_at_ready_prompt.side_effect = _ready_only_at(n_before + 1)
        backend.session.account_pending_reason.return_value = "verifying your account"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch.object(orch, "_recover_broken_pane"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        account_warnings = [m for m in warnings if "[account-pending]" in m]
        assert len(account_warnings) == 1
        # The account-pending check no longer even reads is_at_ready_prompt's
        # verdict, so a single flicker part-way through does not add any
        # extra polls before confirming — it fires at exactly the streak
        # threshold regardless of where the flicker landed.
        assert backend.session.account_pending_reason.call_count == _AUTH_FAILURE_CONFIRM_POLLS


class TestAccountPendingRequiresConsecutivePolls:
    def test_single_poll_match_never_fires(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        assert _AUTH_FAILURE_CONFIRM_POLLS > 1
        n = _AUTH_FAILURE_CONFIRM_POLLS - 1
        backend.session.account_pending_reason.side_effect = _marker_for(
            n, "verifying your account"
        )
        backend.session.is_at_ready_prompt.side_effect = _ready_after(n)
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[account-pending]" in m for m in warnings)
        assert backend.session.write.called  # delivered normally once ready

    def test_confirm_poll_streak_fires_exactly_once(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = False
        backend.session.account_pending_reason.return_value = "verifying your account"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            # Mock the SHARED close+respawn mechanic (same reasoning as the
            # test above) so _recover_account_pending_pane's own body — the
            # Lead notice being asserted on — actually runs.
            patch.object(orch, "_recover_broken_pane") as recover_broken,
            patch("agent_takkub.lead_inbox._log_event") as log,
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        account_warnings = [m for m in warnings if "[account-pending]" in m]
        assert len(account_warnings) == 1
        assert "backend" in account_warnings[0]
        assert "verifying your account" in account_warnings[0]
        # Must not tell Lead to log back in — the message may still SAY
        # "not a login/credentials problem" (that's the point being made),
        # it must just never instruct re-authentication as the fix.
        assert "log back in" not in account_warnings[0].lower()
        assert "ไม่ใช่ปัญหา login" in account_warnings[0]
        assert backend.session.account_pending_reason.call_count == _AUTH_FAILURE_CONFIRM_POLLS
        assert any(
            c.args and c.args[0] == "task_deliver_account_pending" for c in log.call_args_list
        )
        # Never blind-pastes into the gated pane — routes to close+respawn+
        # degrade instead, same shape as the auth-failure recovery.
        recover_broken.assert_called_once()
        call = recover_broken.call_args
        assert call.args[0] == "backend"
        assert call.kwargs["degrade"] is True
        assert call.kwargs["kind"] == "account_pending"
        assert not backend.session.write.called


class TestUngatedMarkerHoldsTheReadyStreak:
    """(#376) `PtySession.shows_account_pending_marker` — UNGATED, unlike
    `account_pending_reason` above — must hold the ready-streak at 0 on its
    own, independent of `AUTH_TRANSIENT_GRACE_SEC`/`_AUTH_FAILURE_CONFIRM_POLLS`.
    Reproduces the exact #376 incident: `is_at_ready_prompt()` pinned True
    (the #363 misread footer) and `seconds_since_output()` pinned to 0.0
    (long before the 45s grace) — nothing but the new ungated check could be
    holding delivery back here; the pre-#376 code delivered onto this exact
    screen shape within ~7s of spawn."""

    def test_ready_streak_never_advances_while_banner_shows(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        agy = _pane(_live_session())
        agy.session.is_at_ready_prompt.return_value = True
        agy.session.shows_account_pending_marker.side_effect = _true_then_false(10)
        agy.session.account_pending_reason.return_value = None
        agy.session.seconds_since_output.return_value = 0.0
        orch._panes_by_project["P"] = {"lead": lead, "agy": agy}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("agy", "run smoke", max_wait_ms=100_000, project="P")

        assert agy.session.write.called
        # Delivery can only happen once 7 CONSECUTIVE clear polls piled up
        # AFTER the banner's own 10-poll block (required_polls for
        # max_wait_ms=100_000 is 7) — i.e. no sooner than poll 16 — proving
        # the gate, not luck, held the streak at 0 the whole time the banner
        # showed. Pre-#376 code ignored the banner entirely and could
        # deliver as early as poll 7.
        assert agy.session.shows_account_pending_marker.call_count >= 16

    def test_provider_with_no_marker_confirmed_is_unaffected(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        # shows_account_pending_marker() is a no-op (always False) for a
        # provider with no confirmed markers (e.g. claude) — delivery must
        # behave exactly as before #376 for it.
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.model = MagicMock(provider_name="claude")
        backend.session.is_at_ready_prompt.return_value = True
        backend.session.shows_account_pending_marker.return_value = False
        backend.session.seconds_since_output.return_value = 0.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=1000, project="P")

        assert backend.session.write.called


class TestNeverBlindPasteIntoAccountPendingBanner:
    """(#376) The last-resort blind paste at the hard delivery timeout must
    not fire while a pane is frozen on its provider's own account-pending
    banner — same defer-then-force-paste contract `_deliver()` already
    applies to a trust/tty prompt (see test_delivery_blocked_prompt.py's
    TestNeverBlindPasteIntoModal, which this mirrors)."""

    def test_gives_up_without_pasting_once_banner_persists_past_ceiling(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#484: the OLD contract ("still eventually pastes, last resort")
        was itself the bug, same class of live incident as the trust-modal
        case this mirrors (test_delivery_blocked_prompt.py) — a confirmed
        account-pending gate must never be pasted into either, however long
        it has been deferred. Gives up cleanly instead."""
        import agent_takkub.lead_inbox as li

        # Small defer ceiling, same technique as the trust-modal test this
        # mirrors — the synchronous singleShot stub recurses instead of
        # returning to an event loop.
        monkeypatch.setattr(li, "_PROMPT_BLOCK_DEFER_CEILING_MS", 300)
        lead = _pane(_live_session())
        agy = _pane(_live_session())
        agy.session.is_at_ready_prompt.return_value = False  # never reaches the ready path
        agy.session.shows_account_pending_marker.return_value = True  # never clears
        agy.session.account_pending_reason.return_value = None  # escalation stays disarmed
        agy.session.seconds_since_output.return_value = float("inf")  # "stuck" path
        orch._panes_by_project["P"] = {"lead": lead, "agy": agy}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("agy", "run smoke", max_wait_ms=300, project="P")

        assert any(
            c.args and c.args[0] == "task_deliver_prompt_defer_ceiling" for c in log.call_args_list
        )
        # Never pastes into the still-confirmed gate — no bytes land on it.
        assert not agy.session.write.called
        warnings = _written_strings(lead.session)
        assert any("[delivery-blocked-ceiling]" in m for m in warnings)
        assert not any("[delivery-unconfirmed]" in m for m in warnings)

    def test_delivers_normally_once_banner_clears_before_defer_ceiling(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The common case: the gate clears well inside the defer ceiling —
        delivery proceeds normally, not via a blind paste at all."""
        lead = _pane(_live_session())
        agy = _pane(_live_session())
        agy.session.is_at_ready_prompt.side_effect = _ready_after(3)
        agy.session.shows_account_pending_marker.side_effect = _true_then_false(3)
        agy.session.account_pending_reason.return_value = None
        agy.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "agy": agy}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("agy", "run smoke", max_wait_ms=100_000, project="P")

        assert agy.session.write.called
        warnings = _written_strings(lead.session)
        assert not any("[delivery-unconfirmed]" in m for m in warnings)
