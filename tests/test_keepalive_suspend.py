"""Tab-visibility keep-alive suspend/resume contract.

A pane in a HIDDEN project tab is never on screen, so the cockpit suspends its
paint keep-alive (heartbeat + xterm.js rAF pulse) to let Chromium reclaim the
renderer's compositor memory — the fix for backgrounded-tab QtWebEngineProcess
renderers ballooning to multi-GB.

These tests exercise the propagation logic (ProjectTab → panes) and the
attach-time inheritance WITHOUT constructing a real QWebEngineView — a fake
pane records set_keepalive() calls. The TerminalWidget side (heartbeat timer
start/stop + JS bridge call) is intentionally not instantiated here: spawning
a real QtWebEngineProcess in the suite is flaky cross-platform (see the note in
test_terminal_widget.py), and the timer/JS logic is a thin, linear wrapper.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub.project_tab import ProjectTab


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakePane(QWidget):
    """Stand-in for AgentPane: a real QWidget (so it can sit in the splitter
    like attach_lead expects) that just records keep-alive toggles."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[bool] = []

    def set_keepalive(self, active: bool) -> None:
        self.calls.append(bool(active))


class TestProjectTabPropagation:
    def _tab(self, qapp) -> ProjectTab:
        return ProjectTab("proj", lead_pane=None)

    def test_defaults_visible(self, qapp):
        tab = self._tab(qapp)
        assert tab._keepalive is True

    def test_suspend_propagates_to_lead_and_teammates(self, qapp):
        tab = self._tab(qapp)
        lead, t1, t2 = _FakePane(), _FakePane(), _FakePane()
        tab.lead_pane = lead
        tab.teammate_panes = {"qa": t1, "backend": t2}

        tab.set_keepalive(False)

        assert tab._keepalive is False
        assert lead.calls == [False]
        assert t1.calls == [False]
        assert t2.calls == [False]

    def test_resume_propagates(self, qapp):
        tab = self._tab(qapp)
        lead = _FakePane()
        tab.lead_pane = lead
        tab.set_keepalive(False)
        tab.set_keepalive(True)
        assert tab._keepalive is True
        assert lead.calls == [False, True]

    def test_suspend_with_no_lead_is_safe(self, qapp):
        """A tab whose Lead hasn't been attached yet (deferred-attach window)
        must not raise when suspended."""
        tab = self._tab(qapp)
        tab.set_keepalive(False)  # lead_pane is None
        assert tab._keepalive is False


class TestAttachInheritsState:
    def test_lead_attached_into_hidden_tab_starts_suspended(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        tab.set_keepalive(False)  # tab hidden before Lead exists
        lead = _FakePane()
        tab.attach_lead(lead)
        # attach_lead must push the hidden state onto the freshly-attached Lead.
        assert lead.calls == [False]

    def test_lead_attached_into_visible_tab_not_forced(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead = _FakePane()
        tab.attach_lead(lead)
        # Visible tab: no redundant set_keepalive(True) — pane defaults to on.
        assert lead.calls == []
