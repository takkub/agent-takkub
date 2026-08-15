"""Tests proving the #248/#247 round-2 no-content watchdog's 1-retry +
1-degrade cap is really a HARD cap of 2 — `_send_when_ready`'s `_check`
closure reads `PaneState.no_content_recover_attempts` (persisted so it
survives the close()/respawn cycle a recovery triggers) and must stop
recovering once it reaches 2, falling through to the ordinary
`elapsed[0] >= max_wait_ms` blind-deliver safety net instead of retrying
forever."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _exit_key


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
    s.auth_failure_reason.return_value = None
    return s


def _no_content_session() -> MagicMock:
    """A teammate pane's session: never ready, never rendered any content —
    the exact no-content-watchdog trigger condition. NOT for the lead's own
    session — `_notify_lead` spills into a queue instead of writing directly
    when the lead pane itself doesn't look ready, so the lead mock must stay
    plain `_live_session()` (ready by MagicMock-truthy default) or the test
    can't observe the notice at all."""
    s = _live_session()
    s.is_at_ready_prompt.return_value = False
    s.first_content_ts.return_value = None  # no content ever rendered
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model = MagicMock(provider_name="claude")
    return p


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
    # Small ceiling so the watchdog trips within a couple of 150ms polls
    # instead of the real 75s default.
    monkeypatch.setattr(orch_mod, "NO_CONTENT_WATCHDOG_SEC", 0.1)
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    return o


class TestAttemptZeroRetries:
    def test_first_no_content_timeout_retries_without_degrading(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        backend = _pane(_no_content_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        assert orch._ps(_exit_key("P", "backend")).no_content_recover_attempts == 0

        with (
            patch.object(orch, "_recover_no_content_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        recover.assert_called_once()
        call = recover.call_args
        assert call.args[0] == "backend"
        assert call.kwargs["degrade"] is False


class TestAttemptOneDegrades:
    def test_second_no_content_timeout_degrades_to_claude(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        backend = _pane(_no_content_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        orch._ps(_exit_key("P", "backend")).no_content_recover_attempts = 1

        with (
            patch.object(orch, "_recover_no_content_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=100_000, project="P")

        recover.assert_called_once()
        assert recover.call_args.kwargs["degrade"] is True


class TestAttemptTwoCapsOut:
    def test_third_no_content_timeout_does_not_recover_again(self, orch: Orchestrator) -> None:
        """Cap already spent (1 retry + 1 degrade) — must NOT call
        `_recover_no_content_pane` a third time. Falls through to the
        ordinary hard-timeout blind-deliver path instead (proven here via a
        stale `seconds_since_output` so it resolves in exactly 2 polls
        rather than riding out the busy-wait ceiling)."""
        lead = _pane(_live_session())
        backend = _pane(_no_content_session())
        backend.session.seconds_since_output.return_value = 999_999.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        orch._ps(_exit_key("P", "backend")).no_content_recover_attempts = 2

        with (
            patch.object(orch, "_recover_no_content_pane") as recover,
            patch("agent_takkub.lead_inbox._log_event"),
        ):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=300, project="P")

        recover.assert_not_called()
        # Falls through to the ordinary unconfirmed blind-deliver safety net.
        assert backend.session.write.called
        assert any("[delivery-unconfirmed]" in m for m in _written_strings(lead.session))
