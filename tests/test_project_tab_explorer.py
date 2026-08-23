"""ProjectTab's QSplitter + Project Explorer wiring (#365 phase 1):
collapse/expand, per-project width persistence, and graceful degrade when
explorer construction fails — plus the split-out `self.pane_tabs` attribute
staying intact for `tests/test_keepalive_suspend.py` and `test_project_nav.py`.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import project_tab
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
    finalize_tab = stop_timers_after(
        monkeypatch, ProjectTab, "_tab_status_timer", "_explorer_save_timer"
    )
    finalize_git = stop_timers_after(monkeypatch, GitStatusService, "_timer")
    yield
    finalize_tab()
    try:
        finalize_git()
    except RuntimeError:
        pass  # explorer (and its child GitStatusService/QTimer) already GC'd


@pytest.fixture
def isolated_qsettings(monkeypatch, tmp_path):
    """Redirect ProjectTab's `QSettings("agent-takkub", "cockpit")` calls to
    a throwaway per-test INI file — without this, tests would read/write the
    real machine's registry/INI store under the same org/app MainWindow uses
    for window geometry."""
    ini_path = str(tmp_path / "cockpit_settings.ini")

    def _factory(*_args, **_kwargs):
        return QSettings(ini_path, QSettings.Format.IniFormat)

    monkeypatch.setattr(project_tab, "QSettings", _factory)
    return ini_path


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
    def test_explorer_and_splitter_present_by_default(self, qapp, isolated_qsettings) -> None:
        tab = ProjectTab("proj-a")
        assert tab.explorer is not None
        assert tab.splitter is not None
        assert tab.splitter.indexOf(tab.explorer) == 0
        assert tab.splitter.indexOf(tab.pane_tabs) == 1

    def test_pane_tabs_attribute_still_works_for_lead_and_teammates(
        self, qapp, isolated_qsettings
    ) -> None:
        # Same contract test_keepalive_suspend.py / test_project_nav.py rely on.
        tab = ProjectTab("proj-a")
        lead = _FakePane()
        tab.attach_lead(lead)
        assert tab.pane_tabs.indexOf(lead) == 0
        qa = _FakePane()
        tab.add_teammate_tab("qa", qa, "qa")
        assert tab.pane_tabs.indexOf(qa) == 1

    def test_degrades_gracefully_when_explorer_init_raises(
        self, qapp, isolated_qsettings, monkeypatch
    ) -> None:
        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(project_tab, "ProjectExplorer", _boom)

        tab = ProjectTab("proj-degraded")

        assert tab.explorer is None
        assert tab.splitter is None
        # pane_tabs still fully functional without the explorer.
        lead = _FakePane()
        tab.attach_lead(lead)
        assert tab.pane_tabs.indexOf(lead) == 0


class TestCollapseExpand:
    def test_toggle_collapses_and_expands(self, qapp, isolated_qsettings) -> None:
        tab = ProjectTab("proj-b")
        assert tab.is_explorer_collapsed() is False

        tab.set_explorer_collapsed(True)
        assert tab.is_explorer_collapsed() is True
        assert tab.explorer.isHidden() is True

        tab.set_explorer_collapsed(False)
        assert tab.is_explorer_collapsed() is False
        assert tab.explorer.isHidden() is False

    def test_toggle_button_click_flips_state(self, qapp, isolated_qsettings) -> None:
        tab = ProjectTab("proj-b2")
        assert tab.is_explorer_collapsed() is False
        tab._explorer_toggle_btn.click()
        assert tab.is_explorer_collapsed() is True
        tab._explorer_toggle_btn.click()
        assert tab.is_explorer_collapsed() is False


class TestPersistence:
    def test_collapsed_state_persists_across_instances(self, qapp, isolated_qsettings) -> None:
        tab1 = ProjectTab("proj-persist")
        tab1.set_explorer_collapsed(True)

        tab2 = ProjectTab("proj-persist")
        assert tab2.is_explorer_collapsed() is True

    def test_expanded_state_persists_across_instances(self, qapp, isolated_qsettings) -> None:
        tab1 = ProjectTab("proj-persist-2")
        tab1.set_explorer_collapsed(True)
        tab1.set_explorer_collapsed(False)

        tab2 = ProjectTab("proj-persist-2")
        assert tab2.is_explorer_collapsed() is False

    def test_width_persists_across_instances(self, qapp, isolated_qsettings) -> None:
        tab1 = ProjectTab("proj-persist-w")
        tab1.explorer.resize(333, 600)
        tab1._persist_explorer_width()

        tab2 = ProjectTab("proj-persist-w")
        assert tab2._explorer_last_width == 333

    def test_different_projects_do_not_share_state(self, qapp, isolated_qsettings) -> None:
        tab_a = ProjectTab("proj-x")
        tab_a.set_explorer_collapsed(True)

        tab_b = ProjectTab("proj-y")
        assert tab_b.is_explorer_collapsed() is False

    def test_splitter_move_starts_debounce_save_timer(self, qapp, isolated_qsettings) -> None:
        tab = ProjectTab("proj-debounce")
        assert not tab._explorer_save_timer.isActive()
        tab._on_splitter_moved(100, 1)
        assert tab._explorer_save_timer.isActive()

    def test_splitter_move_while_collapsed_does_not_start_timer(
        self, qapp, isolated_qsettings
    ) -> None:
        tab = ProjectTab("proj-debounce-2")
        tab.set_explorer_collapsed(True)
        tab._on_splitter_moved(100, 1)
        assert not tab._explorer_save_timer.isActive()
