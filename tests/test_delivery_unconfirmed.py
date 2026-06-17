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


class TestVerifiedEnterWiring:
    """The swallowed-Enter self-heal (#22) must cover teammate-bound deliveries,
    not just Lead notices — _send_when_ready's task paste and the peer `send`
    paste are the documented victims (a pane stuck on `[Pasted text]` forever).
    Both must route the submit through _delayed_enter_verified, never plain
    _delayed_enter."""

    def test_task_deliver_uses_verified_enter(self, orch: Orchestrator, monkeypatch) -> None:
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = True  # ready → deliver now
        orch._panes_by_project["P"] = {"lead": _pane(_live_session()), "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
            patch("agent_takkub.orchestrator._delayed_enter") as plain,
        ):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")
        verified.assert_called_once()
        assert verified.call_args[0][1] is reviewer.session
        plain.assert_not_called()

    def test_peer_send_uses_verified_enter(self, orch: Orchestrator, monkeypatch) -> None:
        reviewer = _pane(_live_session())
        orch._panes_by_project["P"] = {"reviewer": reviewer}
        monkeypatch.setattr(orch, "_ps", lambda key: MagicMock())
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
            patch("agent_takkub.orchestrator._delayed_enter") as plain,
        ):
            ok, _ = orch.send("reviewer", "hello peer", project="P")
        assert ok
        verified.assert_called_once()
        assert verified.call_args[0][1] is reviewer.session
        plain.assert_not_called()


class TestSpawnFailureNotSilent:
    """#26 root cause: when spawn can't register the pane (main_window routing
    desync), assign must NOT silently drop — it logs and warns the Lead."""

    def test_spawn_logs_and_returns_false_when_pane_absent(self, orch: Orchestrator) -> None:
        # No main_window is connected to paneRequested, so the pane never gets
        # created/registered — spawn must surface that, not return a bare False.
        orch._idle_state = {}
        orch._pane_state = {}
        with patch("agent_takkub.orchestrator._log_event") as log:
            ok, msg = orch.spawn("reviewer", cwd=None, project="P")
        assert ok is False
        assert "could not create pane" in msg
        assert any(c.args and c.args[0] == "spawn_failed" for c in log.call_args_list)

    def test_assign_warns_lead_when_spawn_fails(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        orch._idle_state = {}
        orch._pane_state = {}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            ok, _msg = orch.assign("reviewer", cwd=None, task="do it", project="P")
        assert ok is False
        assert any("spawn-failed" in c.args[0] for c in lead.session.write.call_args_list if c.args)

    def test_warn_spawn_failed_noop_for_lead_role(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_spawn_failed("lead", "P", "x")
        lead.session.write.assert_not_called()

    def test_warn_spawn_failed_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"reviewer": _pane(_live_session())}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_spawn_failed("reviewer", "P", "x")  # must not raise
