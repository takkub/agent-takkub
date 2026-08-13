"""Tests for the busy-wait first-entry Lead notice (issue #144).

Before this, the Lead heard nothing about an assign stuck in the #130
busy-wait extension (target pane past its normal delivery timeout but still
producing output) until either the task finally landed or the
BUSY_WAIT_CEILING_SEC absolute ceiling fired — up to 30 minutes later by
default. `_warn_lead_delivery_busy_wait` now fires once, right as the
extension begins, so the Lead knows delivery is still in flight.
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
    # #186: is_at_trust_prompt()/is_blocked_on_tty_prompt() are checked on
    # every delivery poll — an unconfigured MagicMock call returns a
    # (truthy) MagicMock, which would misread every test pane below as
    # blocked-on-prompt. Real PtySession returns bool/None for an ordinary
    # non-blocked pane, so pin the mocks to match.
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
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


class TestBusyWaitFirstEntryNotice:
    def test_warns_lead_once_when_extension_begins(self, orch: Orchestrator, monkeypatch) -> None:
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        # Not ready for 6 polls (past the 300ms hard timeout at poll 2), then
        # ready — the extension window is polls 2..5.
        reviewer.session.is_at_ready_prompt.side_effect = [False] * 6 + [True] * 10
        reviewer.session.seconds_since_output.return_value = 2.0  # busy, not stuck
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        busy_wait_warnings = [m for m in warnings if "[delivery-busy-wait]" in m]
        assert len(busy_wait_warnings) == 1, "must warn exactly once per delivery, not per poll"
        assert "reviewer" in busy_wait_warnings[0]
        assert "#144" in busy_wait_warnings[0]

    def test_no_busy_wait_warning_when_pane_ready_immediately(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = True  # ready right away
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")

        assert not any("[delivery-busy-wait]" in m for m in _written_strings(lead.session))

    def test_busy_wait_warning_precedes_ceiling_warning_and_stays_singular(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """Both the early (#144) and the ceiling (#131) warnings must fire for
        a pane that stays busy the whole way to the ceiling — but the early
        one only once, not on every poll leading up to it."""
        monkeypatch.setattr(orch_mod, "BUSY_WAIT_CEILING_SEC", 1)  # 1s ceiling — fake clock
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = False  # never ready
        reviewer.session.seconds_since_output.return_value = 1.0  # busy the whole time
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        assert sum("[delivery-busy-wait]" in m for m in warnings) == 1
        assert any("[delivery-unconfirmed]" in m and "#131" in m for m in warnings)

    def test_no_busy_wait_warning_for_truly_stuck_pane(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A pane that's silent (not busy) past STALL_THRESHOLD_SEC skips the
        busy-wait branch entirely — it gets the plain #26 warning instead, not
        the busy-wait one."""
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = False
        reviewer.session.seconds_since_output.return_value = float("inf")  # stuck, not busy
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[delivery-busy-wait]" in m for m in warnings)
        assert any("[delivery-unconfirmed]" in m and "#26" in m for m in warnings)


class TestWarnLeadDeliveryBusyWaitDirect:
    def test_message_contains_marker_role_and_seconds(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", "P", 12.3)
        msg = lead.session.write.call_args[0][0]
        assert "[delivery-busy-wait]" in msg
        assert "backend" in msg
        assert "12" in msg
        assert "#144" in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("lead", "P", 5.0)
        lead.session.write.assert_not_called()

    def test_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"backend": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", "P", 5.0)  # must not raise
