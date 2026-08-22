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
  4. Let a ready prompt win over a lingering marker, same "screen is
     definitive" reasoning as the auth-failure tier.
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


class TestReadyPromptWinsOverStaleAccountMarker:
    def test_ready_prompt_delivers_normally_despite_marker_every_poll(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = True
        backend.session.account_pending_reason.return_value = "verifying your account"
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[account-pending]" in m for m in warnings)
        assert backend.session.write.called

    def test_ready_prompt_resets_streak_so_a_later_relapse_needs_its_own_confirmation(
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
            # Mock the SHARED close+respawn mechanic, not
            # _recover_account_pending_pane itself — that method's own body
            # is what fires the Lead notice being asserted on below.
            patch.object(orch, "_recover_broken_pane"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        warnings = _written_strings(lead.session)
        account_warnings = [m for m in warnings if "[account-pending]" in m]
        assert len(account_warnings) == 1
        assert (
            backend.session.account_pending_reason.call_count
            == n_before + _AUTH_FAILURE_CONFIRM_POLLS
        )


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
