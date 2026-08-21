"""Tests for the UNCERTAIN-delivery Lead notice (2026-08-21).

`DeliveryState.UNCERTAIN` used to be a dead end: `_on_settled` called
`mark_uncertain()` and nothing else ever happened — no escalation, no reaper,
no notice. The Lead kept believing the pane was working while the task sat
unsent in its composer, and `takkub status` showed a pane that looked busy.
Reported from a gemini pane that hit it twice in one session; gemini gets
there more often than claude only because it cannot preload a task into the
spawn (`system_prompt_flag is None`) and so always rides the paste path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

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


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


class TestUncertainDeliveryNotice:
    def test_lead_is_told_the_task_may_never_have_been_submitted(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": _pane(_live_session())}

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_uncertain("gemini", "P")

        written = " ".join(_written_strings(lead.session))
        assert "delivery-uncertain" in written
        assert "gemini" in written

    def test_notice_names_what_the_operator_has_to_do(self, orch: Orchestrator) -> None:
        """The whole point is that nothing downstream retries this — the
        message has to say so, or it reads as one more transient warning."""
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": _pane(_live_session())}

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_uncertain("gemini", "P")

        written = " ".join(_written_strings(lead.session))
        assert "assign" in written  # tells the reader to re-assign
        assert "#134" in written  # and why the cockpit will not repaste itself

    def test_logs_an_event_so_the_state_is_greppable_after_the_fact(
        self, orch: Orchestrator
    ) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": _pane(_live_session())}

        with patch("agent_takkub.lead_inbox._log_event") as log_event:
            orch._warn_lead_delivery_uncertain("gemini", "P")

        assert any(
            c.args and c.args[0] == "delivery_uncertain_warned" for c in log_event.mock_calls
        )

    def test_never_warns_the_lead_about_itself(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_uncertain("lead", "P")

        assert not _written_strings(lead.session)

    def test_no_live_lead_pane_is_a_silent_noop(self, orch: Orchestrator) -> None:
        """Delivery must never blow up because there is nobody to tell."""
        dead = _pane(_live_session())
        dead.session.is_alive = False
        orch._panes_by_project["P"] = {"lead": dead, "gemini": _pane(_live_session())}

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_uncertain("gemini", "P")

        assert not _written_strings(dead.session)
