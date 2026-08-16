"""Tests for the boot-stall Lead notice (issue #254).

A provider CLI's boot-phase spinner (e.g. codex "Booting MCP server: … (0s •
esc to interrupt)") redraws continuously, so `seconds_since_output()` never
grows and the generic #144 busy-wait extension silently renews itself with
no further signal until BUSY_WAIT_CEILING_SEC (30 min default) — a real
incident sat exactly there with the task never even pasted. This adds an
earlier, distinct escalation (`_warn_lead_delivery_boot_stall`) that fires
once the target pane shows `PtySession.shows_startup_marker()` on every poll
for BOOT_STALL_GRACE_SEC straight, well before the generic ceiling.
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
    s.shows_startup_marker.return_value = False
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
    # #273: `_warn_lead_delivery_boot_stall` now ALSO fires an async
    # `codex mcp list`-style diagnostic probe. This file tests the notice
    # text only — the diagnostic follow-up has its own dedicated tests
    # (test_boot_diagnostic.py). Without this no-op, a dev machine that
    # actually has codex on PATH would spawn a REAL subprocess here (role
    # "codex" is forced to provider codex), which is both an unwanted side
    # effect and a real source of test flakiness/crashes across the suite.
    monkeypatch.setattr(o, "_run_boot_diagnostic_async", MagicMock())
    return o


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


class TestBootStallNotice:
    def test_warns_lead_once_after_continuous_boot_marker_streak(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        monkeypatch.setattr(orch_mod, "BOOT_STALL_GRACE_SEC", 1)  # ~7 polls at 150ms
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.side_effect = [False] * 8 + [True] * 10
        codex.session.shows_startup_marker.side_effect = [True] * 8 + [False] * 10
        codex.session.seconds_since_output.return_value = 2.0  # busy, not stuck
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        boot_stall_warnings = [m for m in warnings if "[delivery-boot-stall]" in m]
        assert len(boot_stall_warnings) == 1, "must warn exactly once per delivery, not per poll"
        assert "codex" in boot_stall_warnings[0]
        assert "#254" in boot_stall_warnings[0]

    def test_no_boot_stall_warning_for_ordinary_busy_pane(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A pane that's simply busy (never shows the boot marker) must only
        ever get the generic #144 notice, not the boot-stall one."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_GRACE_SEC", 1)
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.side_effect = [False] * 6 + [True] * 10
        reviewer.session.shows_startup_marker.return_value = False
        reviewer.session.seconds_since_output.return_value = 2.0
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[delivery-boot-stall]" in m for m in warnings)
        assert any("[delivery-busy-wait]" in m for m in warnings)

    def test_intermittent_boot_marker_never_accumulates(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """An alternating (flaky-render) boot marker must reset the streak
        each time it drops out, never firing the escalation."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_GRACE_SEC", 1)  # needs ~7 CONSECUTIVE polls
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.side_effect = [False] * 8 + [True] * 10
        # True, False alternating — never more than 1 consecutive hit.
        codex.session.shows_startup_marker.side_effect = [True, False] * 4 + [False] * 10
        codex.session.seconds_since_output.return_value = 2.0
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[delivery-boot-stall]" in m for m in warnings)

    def test_unconfigured_mock_session_never_falsely_trips(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A session double that never pins shows_startup_marker() (an
        unconfigured MagicMock returns a truthy Mock by default) must not be
        misread as a genuine boot-phase hit — regression guard for the
        isinstance(bool) defensive cast."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_GRACE_SEC", 1)
        lead = _pane(_live_session())
        reviewer = _pane(MagicMock())
        reviewer.session.is_alive = True
        reviewer.session.is_at_trust_prompt.return_value = False
        reviewer.session.is_blocked_on_tty_prompt.return_value = None
        reviewer.session.is_blocked_on_permission_prompt.return_value = None
        # shows_startup_marker deliberately left UNCONFIGURED.
        reviewer.session.is_at_ready_prompt.return_value = True  # ready right away
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")

        warnings = _written_strings(lead.session)
        assert not any("[delivery-boot-stall]" in m for m in warnings)


class TestWarnLeadDeliveryBootStallDirect:
    def test_message_contains_marker_role_and_issue(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "codex": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_boot_stall("codex", "P", 120.0)
        msg = lead.session.write.call_args[0][0]
        assert "[delivery-boot-stall]" in msg
        assert "codex" in msg
        assert "#254" in msg
        assert "takkub close --role codex" in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_boot_stall("lead", "P", 120.0)
        lead.session.write.assert_not_called()

    def test_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"codex": _pane(_live_session())}
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_boot_stall("codex", "P", 120.0)  # must not raise
