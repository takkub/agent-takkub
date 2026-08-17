"""#269: an auth-failure marker (any provider) must degrade a pane to claude
the same way the no-content watchdog already does — before this fix, ONLY
the no-content path ever reached `PaneState.provider_override`, so a role
whose provider CLI renders a recognisable auth-failure marker (e.g. gemini's
"not signed in") never degraded at all, no matter how many times Lead
closed+reassigned it.

Covers:
  1. A role with an explicit non-default provider (mirrors a role-providers.json
     override, e.g. qa -> gemini-agy) confirmed auth-failed -> degrades.
  2. A role using the default ("claude") provider confirmed auth-failed ->
     degrades identically — proves the recovery is provider/role-agnostic,
     not special-cased for either side.
  3. `_recover_auth_failed_pane` always degrades on its FIRST call (no
     non-degraded retry step, unlike the no-content watchdog) and shares
     `_recover_broken_pane`'s close+respawn+`provider_override` mechanics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_inbox import _AUTH_FAILURE_CONFIRM_POLLS
from agent_takkub.orchestrator import Orchestrator, _exit_key


@pytest.fixture(autouse=True)
def _live_watch_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    """#280 moved these watchdog observations out of immediate Lead notices
    and into the pane's own end-of-life report.

    This file tests the DETECTION — does the watchdog fire at the right
    moment, about the right role, with the right wording — none of which
    #280 changed; only the moment Lead is told did. `live` is the policy
    under which that message is still rendered verbatim, so every assertion
    below keeps testing exactly what it was written to test. The new routing
    has its own coverage in tests/test_pane_health_reporting.py and
    tests/test_pane_health_close_report.py.
    """
    monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "live")


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


def _auth_failed_session(reason: str = "not signed in") -> MagicMock:
    s = _live_session()
    s.is_at_ready_prompt.return_value = False
    s.auth_failure_reason.return_value = reason
    s.seconds_since_output.return_value = 1.0
    return s


def _pane(session=None, *, provider: str = "claude") -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model = MagicMock(provider_name=provider)
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    return o


class TestCheckRoutesAuthFailureToDegrade:
    def test_role_with_provider_override_degrades(self, orch: Orchestrator) -> None:
        """Mirrors a role-providers.json override (e.g. qa -> gemini-agy) —
        the provider name alone must not gate whether degrade fires."""
        lead = _pane(_live_session())
        qa = _pane(_auth_failed_session(), provider="gemini-agy")
        orch._panes_by_project["P"] = {"lead": lead, "qa": qa}

        with (
            patch.object(orch, "_recover_auth_failed_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("qa", "run tests", max_wait_ms=100_000, project="P")

        recover.assert_called_once()
        call = recover.call_args
        assert call.args[0] == "qa"
        assert call.kwargs["provider"] == "gemini-agy"
        assert call.kwargs["reason"] == "not signed in"

    def test_role_with_default_provider_degrades_identically(self, orch: Orchestrator) -> None:
        """Same fire, same call shape, for a role that never had any
        provider override — proves no role gets a special path either way."""
        lead = _pane(_live_session())
        backend = _pane(_auth_failed_session(), provider="claude")
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}

        with (
            patch.object(orch, "_recover_auth_failed_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run tests", max_wait_ms=100_000, project="P")

        recover.assert_called_once()
        assert recover.call_args.kwargs["provider"] == "claude"

    def test_fires_after_exactly_the_confirm_poll_streak_not_before(
        self, orch: Orchestrator
    ) -> None:
        """Regression guard for #256/#257: the consecutive-poll confirmation
        gate itself must survive this change untouched."""
        lead = _pane(_live_session())
        qa = _pane(_auth_failed_session(), provider="gemini-agy")
        orch._panes_by_project["P"] = {"lead": lead, "qa": qa}

        with (
            patch.object(orch, "_recover_auth_failed_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("qa", "run tests", max_wait_ms=100_000, project="P")

        assert qa.session.auth_failure_reason.call_count == _AUTH_FAILURE_CONFIRM_POLLS
        recover.assert_called_once()


class TestRecoverAuthFailedPaneDegradesImmediately:
    def test_first_call_degrades_with_no_prior_attempts(self, orch: Orchestrator) -> None:
        """Unlike the no-content watchdog (one plain retry before it
        degrades), an auth-failure recovery degrades on attempt zero — a
        login wall does not clear itself on a same-provider retry."""
        lead = _pane(_live_session())
        qa = _pane(_auth_failed_session(), provider="gemini-agy")
        orch._panes_by_project["P"] = {"lead": lead, "qa": qa}
        key = _exit_key("P", "qa")
        assert orch._ps(key).no_content_recover_attempts == 0

        with (
            patch.object(orch, "close") as mock_close,
            patch.object(orch, "spawn", return_value=(True, "ok")) as mock_spawn,
            patch.object(orch, "_send_when_ready") as mock_resend,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._recover_auth_failed_pane(
                "qa", "P", qa, "run tests", provider="gemini-agy", reason="not signed in"
            )

        mock_close.assert_called_once()
        mock_spawn.assert_called_once()
        ps_after = orch._pane_state[key]
        assert ps_after.provider_override == "claude"
        assert ps_after.no_content_recover_attempts == 1
        mock_resend.assert_called_once()

    def test_warns_lead_with_provider_and_reason(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        qa = _pane(_auth_failed_session(), provider="gemini-agy")
        orch._panes_by_project["P"] = {"lead": lead, "qa": qa}

        with (
            patch.object(orch, "close"),
            patch.object(orch, "spawn", return_value=(True, "ok")),
            patch.object(orch, "_send_when_ready"),
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._recover_auth_failed_pane(
                "qa", "P", qa, "run tests", provider="gemini-agy", reason="not signed in"
            )

        warnings = [
            c.args[0]
            for c in lead.session.write.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        degrade_warnings = [m for m in warnings if "[auth-failure-degrade]" in m]
        assert len(degrade_warnings) == 1
        assert "qa" in degrade_warnings[0]
        assert "not signed in" in degrade_warnings[0]
