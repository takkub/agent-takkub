"""Pane-tab keep-alive + unread red-dot contract (panes-as-tabs layout).

After the 2026-06-26 redesign every pane is a tab inside its ProjectTab, and
only the *visible pane of the visible project* paints — everything else
suspends so Chromium can release the renderer's compositor RAM. A red dot lands
on the Lead pane-tab when a notice arrives while the user is on another pane.

These exercise the propagation/state logic with a fake pane (records keep-alive
toggles) — no real QWebEngineView, which is flaky to spawn in the suite (see
test_terminal_widget.py).
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
    """Stand-in for AgentPane: a real QWidget (so it can live in the pane
    QTabWidget) that records keep-alive toggles. `state` = last value seen."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[bool] = []
        self.state: bool | None = None

    def set_keepalive(self, active: bool) -> None:
        self.calls.append(bool(active))
        self.state = bool(active)


def _idx(tab, pane) -> int:
    return tab.pane_tabs.indexOf(pane)


class TestPaneKeepalive:
    def test_spawn_auto_switches_to_new_pane(self, qapp):
        # Spawning must surface the new pane (the user's "nothing shows" fix):
        # add_teammate_tab switches to it so it's visible + painting.
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.set_keepalive(True)
        tab.add_teammate_tab("qa", qa, "qa")
        assert tab.pane_tabs.currentWidget() is qa  # auto-focused
        assert qa.state is True  # visible → paints
        assert lead.state is False  # backgrounded

    def test_visible_project_only_current_pane_alive(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.add_teammate_tab("qa", qa, "qa")
        tab.set_keepalive(True)  # project visible

        tab.pane_tabs.setCurrentIndex(_idx(tab, lead))  # view Lead
        assert lead.state is True  # current pane → paints
        assert qa.state is False  # background pane → suspended

    def test_hidden_project_suspends_every_pane(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.add_teammate_tab("qa", qa, "qa")

        tab.set_keepalive(False)  # project hidden
        assert lead.state is False
        assert qa.state is False

    def test_switching_pane_tab_moves_aliveness(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.add_teammate_tab("qa", qa, "qa")
        tab.set_keepalive(True)

        tab.pane_tabs.setCurrentIndex(_idx(tab, lead))  # view Lead
        assert lead.state is True
        assert qa.state is False
        tab.pane_tabs.setCurrentIndex(_idx(tab, qa))  # back to qa
        assert qa.state is True
        assert lead.state is False

    def test_teammate_added_into_hidden_project_starts_suspended(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead = _FakePane()
        tab.attach_lead(lead)
        tab.set_keepalive(False)  # project hidden
        qa = _FakePane()
        tab.add_teammate_tab("qa", qa, "qa")
        assert qa.state is False  # born suspended even though auto-focused

    def test_remove_teammate_tab_returns_pane(self, qapp):
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.add_teammate_tab("qa", qa, "qa")
        out = tab.remove_teammate_tab("qa")
        assert out is qa
        assert "qa" not in tab.teammate_panes
        assert tab.remove_teammate_tab("qa") is None


class TestLeadUnreadDot:
    def _tab_with_teammate(self):
        tab = ProjectTab("proj", lead_pane=None)
        lead, qa = _FakePane(), _FakePane()
        tab.attach_lead(lead)
        tab.add_teammate_tab("qa", qa, "qa")
        tab.set_keepalive(True)
        return tab, lead, qa

    def test_dot_set_when_viewing_another_pane(self, qapp):
        tab, lead, qa = self._tab_with_teammate()
        tab.pane_tabs.setCurrentIndex(_idx(tab, qa))  # not looking at Lead
        tab.mark_lead_unread()
        assert not tab.pane_tabs.tabIcon(_idx(tab, lead)).isNull()

    def test_no_dot_when_viewing_lead(self, qapp):
        tab, lead, _qa = self._tab_with_teammate()
        tab.pane_tabs.setCurrentIndex(_idx(tab, lead))  # user is on Lead
        tab.mark_lead_unread()
        assert tab.pane_tabs.tabIcon(_idx(tab, lead)).isNull()

    def test_dot_set_when_project_hidden(self, qapp):
        tab, lead, _qa = self._tab_with_teammate()
        tab.set_keepalive(False)  # whole project off-screen
        tab.mark_lead_unread()
        assert not tab.pane_tabs.tabIcon(_idx(tab, lead)).isNull()

    def test_switching_to_lead_clears_dot(self, qapp):
        tab, lead, qa = self._tab_with_teammate()
        tab.pane_tabs.setCurrentIndex(_idx(tab, qa))
        tab.mark_lead_unread()
        assert not tab.pane_tabs.tabIcon(_idx(tab, lead)).isNull()
        tab.pane_tabs.setCurrentIndex(_idx(tab, lead))  # user reads Lead
        assert tab.pane_tabs.tabIcon(_idx(tab, lead)).isNull()
