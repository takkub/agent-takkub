"""ProjectTab's Project Explorer ownership (#365 phase 1; explorer moved into
the sidebar 2026-08-23 — see project_nav.py): ProjectTab still constructs and
signal-wires a `ProjectExplorer` per project, degrades gracefully when
construction fails, but no longer lays it out itself (no QSplitter/toggle
button here any more — that's project_nav.py's job now, see
tests/test_project_nav.py's TestExplorerEmbedding). This file only covers
what's still ProjectTab's own contract: construction + signal forwarding.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import project_tab
from agent_takkub.git_changes_service import GitChangesService
from agent_takkub.project_file_index import GitStatusService
from agent_takkub.project_tab import ProjectTab

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _stop_timers(monkeypatch):
    finalize_tab = stop_timers_after(monkeypatch, ProjectTab, "_tab_status_timer")
    finalize_git = stop_timers_after(monkeypatch, GitStatusService, "_timer")
    finalize_changes = stop_timers_after(monkeypatch, GitChangesService, "_timer")
    yield
    finalize_tab()
    for finalize in (finalize_git, finalize_changes):
        try:
            finalize()
        except RuntimeError:
            pass  # explorer (and its child git service/QTimer) already GC'd


@pytest.fixture(autouse=True)
def _stub_project_roots(monkeypatch, tmp_path):
    """Every ProjectTab in this file constructs a real ProjectExplorer,
    which reads project_roots() — stub it so no test needs a real
    projects.json / DATA_HOME."""
    from agent_takkub import project_explorer as pe

    monkeypatch.setattr(pe, "project_roots", lambda name: {"main": tmp_path})


class _FakePane(QWidget):
    def set_keepalive(self, active: bool) -> None:
        pass


class TestExplorerConstruction:
    def test_explorer_constructed_by_default(self, qapp) -> None:
        tab = ProjectTab("proj-a")
        assert tab.explorer is not None
        # No splitter/panel layout in ProjectTab any more — pane_tabs is the
        # tab's whole content (project_nav.py owns the explorer's display).
        assert not hasattr(tab, "splitter")

    def test_pane_tabs_attribute_still_works_for_lead_and_teammates(self, qapp) -> None:
        # Same contract test_keepalive_suspend.py / test_project_nav.py rely on.
        tab = ProjectTab("proj-a")
        lead = _FakePane()
        tab.attach_lead(lead)
        assert tab.pane_tabs.indexOf(lead) == 0
        qa = _FakePane()
        tab.add_teammate_tab("qa", qa, "qa")
        assert tab.pane_tabs.indexOf(qa) == 1

    def test_degrades_gracefully_when_explorer_init_raises(self, qapp, monkeypatch) -> None:
        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(project_tab, "ProjectExplorer", _boom)

        tab = ProjectTab("proj-degraded")

        assert tab.explorer is None
        # pane_tabs still fully functional without the explorer.
        lead = _FakePane()
        tab.attach_lead(lead)
        assert tab.pane_tabs.indexOf(lead) == 0


class TestExplorerSignalForwarding:
    def test_change_activated_forwards_as_open_diff_requested(self, qapp) -> None:
        tab = ProjectTab("proj-changes")
        received: list[tuple[str, str]] = []
        tab.openDiffRequested.connect(lambda proj, path: received.append((proj, path)))

        tab.explorer.changeActivated.emit("/abs/path/a.py")

        assert received == [("proj-changes", "/abs/path/a.py")]

    def test_file_activated_still_forwards_as_open_file_requested(self, qapp) -> None:
        tab = ProjectTab("proj-files")
        received: list[tuple[str, str]] = []
        tab.openFileRequested.connect(lambda proj, path: received.append((proj, path)))

        tab.explorer.fileActivated.emit("/abs/path/b.py")

        assert received == [("proj-files", "/abs/path/b.py")]

    def test_ask_agent_requested_forwards_project_name_and_path(self, qapp) -> None:
        tab = ProjectTab("proj-ask")
        received: list[tuple[str, str]] = []
        tab.askAgentRequested.connect(lambda proj, path: received.append((proj, path)))

        tab.explorer.askAgentRequested.emit("/abs/path/c.py")

        assert received == [("proj-ask", "/abs/path/c.py")]
