"""Tests for the assign hard-timeout delivery warning (issue #26).

When _send_when_ready polls is_at_ready_prompt() but the pane never signals
ready (cold re-spawn render differs), it pastes blind at the 45s hard timeout.
That paste can be swallowed, leaving the pane empty while the Lead believes the
task landed. The fix surfaces the unconfirmed delivery to the Lead so
delegation stops failing silently.
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
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
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
    return o


class TestDeliveryUnconfirmedWarning:
    def test_warns_lead_with_role_and_issue_ref(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": _pane(_live_session())}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            orch._warn_lead_delivery_unconfirmed("reviewer", "P")
        assert lead.session.write.called
        msg = lead.session.write.call_args[0][0]
        assert "reviewer" in msg
        assert "#26" in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_delivery_unconfirmed("lead", "P")
        lead.session.write.assert_not_called()

    def test_noop_when_no_live_lead(self, orch: Orchestrator) -> None:
        # Lead absent — must not raise.
        orch._panes_by_project["P"] = {"reviewer": _pane(_live_session())}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_delivery_unconfirmed("reviewer", "P")

    def test_hard_timeout_pastes_and_warns(self, orch: Orchestrator, monkeypatch) -> None:
        """End-to-end: a never-ready pane hits the timeout → task pasted
        best-effort AND the Lead is warned (delegation no longer silent)."""
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = False  # never ready
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        # Run scheduled callbacks synchronously so the poll loop completes.
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.orchestrator._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")

        # Best-effort paste still happened on the reviewer pane...
        assert reviewer.session.write.called
        # ...and the Lead got the unconfirmed-delivery warning.
        assert any("#26" in c.args[0] for c in lead.session.write.call_args_list if c.args)
