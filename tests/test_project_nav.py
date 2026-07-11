"""ProjectNav: the sidebar-list + stacked-content replacement for the old top
QTabWidget. Verifies the QTabWidget-compatible API stays in lockstep (row ==
stack index) so MainWindow's ~dozen call sites keep working unchanged.
"""

from __future__ import annotations

import pathlib

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from agent_takkub import project_nav as project_nav_module
from agent_takkub import task_ledger
from agent_takkub.project_nav import ProjectNav
from agent_takkub.project_tab import ProjectTab


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(project_nav_module, "list_project_names", lambda: [])


def _page(text: str) -> QWidget:
    return QLabel(text)


class TestProjectNavApi:
    def test_add_and_count(self, qapp):
        nav = ProjectNav()
        a, b = _page("a"), _page("b")
        assert nav.addTab(a, "alpha") == 0
        assert nav.addTab(b, "beta") == 1
        assert nav.count() == 2
        assert nav.widget(0) is a
        assert nav.widget(1) is b
        assert nav.indexOf(b) == 1

    def test_first_add_auto_selects_row_zero(self, qapp):
        nav = ProjectNav()
        a = _page("a")
        nav.addTab(a, "alpha")
        assert nav.currentIndex() == 0
        assert nav.currentWidget() is a

    def test_set_current_index_switches_stack_and_emits(self, qapp):
        nav = ProjectNav()
        seen = []
        nav.currentChanged.connect(seen.append)
        a, b = _page("a"), _page("b")
        nav.addTab(a, "alpha")
        nav.addTab(b, "beta")
        nav.setCurrentIndex(1)
        assert nav.currentIndex() == 1
        assert nav.currentWidget() is b
        assert seen[-1] == 1

    def test_remove_keeps_list_and_stack_in_lockstep(self, qapp):
        nav = ProjectNav()
        a, b, c = _page("a"), _page("b"), _page("c")
        nav.addTab(a, "alpha")
        nav.addTab(b, "beta")
        nav.addTab(c, "gamma")
        nav.removeTab(1)  # drop beta
        assert nav.count() == 2
        assert nav.widget(0) is a
        assert nav.widget(1) is c
        # list rows still align with stack indices
        assert nav._list.count() == nav._stack.count()

    def test_set_tab_text_and_usage_do_not_raise(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        nav.setTabText(0, "renamed")
        nav.set_usage(0, 0.42)
        nav.set_usage(0, None)
        nav.setTabToolTip(0, "tip")

    def test_insert_tab_at_index(self, qapp):
        nav = ProjectNav()
        a, c = _page("a"), _page("c")
        nav.addTab(a, "alpha")
        nav.addTab(c, "gamma")
        b = _page("b")
        nav.insertTab(1, b, "beta")
        assert nav.widget(1) is b
        assert nav.count() == 3
        assert nav._list.count() == 3


class TestSidebarCollapse:
    def test_toggle_flips_collapsed_state(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        assert nav.is_sidebar_collapsed() is False
        assert nav.toggle_sidebar() is True
        assert nav.is_sidebar_collapsed() is True
        assert nav.toggle_sidebar() is False
        assert nav.is_sidebar_collapsed() is False

    def test_collapse_hides_row_name_keeps_avatar(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "agent takkub")
        row = nav._row_widget(0)
        assert row._avatar.text() == "AG"  # first 2 non-space letters, upper
        assert row._name.isHidden() is False
        nav.set_sidebar_collapsed(True, animate=False)
        assert row._name.isHidden() is True
        assert row._badge.isHidden() is True
        assert nav._sidebar.width() == 64

    def test_rows_added_while_collapsed_start_collapsed(self, qapp):
        nav = ProjectNav()
        nav.set_sidebar_collapsed(True, animate=False)
        nav.addTab(_page("a"), "beta")
        row = nav._row_widget(0)
        assert row._name.isHidden() is True
        assert row._avatar.text() == "BE"

    def test_set_collapsed_is_idempotent(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        nav.set_sidebar_collapsed(False, animate=False)  # already expanded
        assert nav.is_sidebar_collapsed() is False
        nav.set_sidebar_collapsed(True, animate=False)
        nav.set_sidebar_collapsed(True, animate=False)  # no-op
        assert nav.is_sidebar_collapsed() is True


class TestUsageMeterCorner:
    """Usage meter now lives as corner widget on the active ProjectTab's pane_tabs."""

    def test_mount_sets_corner_widget_and_shows_label(self, qapp):
        tab = ProjectTab("proj-a")
        meter = QLabel("5h 12% / 7d 4%")
        tab.mount_usage_widget(meter)
        assert tab.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is meter
        assert meter.isHidden() is False

    def test_reparent_moves_meter_to_new_tab(self, qapp):
        tab_a = ProjectTab("proj-a")
        tab_b = ProjectTab("proj-b")
        meter = QLabel("—")
        tab_a.mount_usage_widget(meter)
        assert tab_a.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is meter
        # Simulate MainWindow's _on_tab_switched: clear old corner before mounting new
        tab_a.pane_tabs.setCornerWidget(None, Qt.Corner.TopRightCorner)
        tab_b.mount_usage_widget(meter)
        assert tab_b.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is meter
        assert tab_a.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is None


class TestUsageBadgeLegend:
    """Walkthrough cluster D item 2: the sidebar '33%' badge had no legend.
    Now carries an icon + a tooltip explaining what it measures."""

    def test_usage_badge_has_icon_and_tooltip(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        row = nav._row_widget(0)
        nav.set_usage(0, 0.33)
        assert "33%" in row._badge.text()
        assert row._badge.toolTip() != ""
        assert "33%" in row._badge.toolTip()

    def test_none_ratio_clears_badge_and_tooltip(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        row = nav._row_widget(0)
        nav.set_usage(0, 0.5)
        nav.set_usage(0, None)
        assert row._badge.text() == ""
        assert row._badge.toolTip() == ""


class TestPendingProjectsSection:
    """Walkthrough cluster D item 1: sidebar only shows open-tab projects but
    the task dock shows every project with ledger rows — mismatched mental
    model. `refresh_pending_projects` surfaces open (`working`) tasks from
    projects that aren't an open tab."""

    def test_hidden_when_nothing_pending(self, qapp):
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        assert nav._pending_header.isVisible() is False
        assert nav._pending_list.isVisible() is False

    def test_project_with_working_row_and_no_open_tab_is_listed(self, qapp, monkeypatch):
        monkeypatch.setattr(project_nav_module, "list_project_names", lambda: ["other-proj"])
        task_ledger.create_assignment(
            "other-proj", "backend", "/api", "add endpoint", None, None, "claude"
        )
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        nav.refresh_pending_projects()
        assert nav._pending_list.count() == 1
        item = nav._pending_list.item(0)
        assert item.data(Qt.ItemDataRole.UserRole) == "other-proj"
        assert nav._pending_header.isHidden() is False

    def test_project_already_open_is_excluded(self, qapp, monkeypatch):
        monkeypatch.setattr(project_nav_module, "list_project_names", lambda: ["alpha"])
        task_ledger.create_assignment(
            "alpha", "backend", "/api", "add endpoint", None, None, "claude"
        )
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")  # already-open tab is named "alpha"
        nav.refresh_pending_projects()
        assert nav._pending_list.count() == 0

    def test_clicking_pending_item_emits_open_project_requested(self, qapp, monkeypatch):
        monkeypatch.setattr(project_nav_module, "list_project_names", lambda: ["other-proj"])
        task_ledger.create_assignment(
            "other-proj", "backend", "/api", "add endpoint", None, None, "claude"
        )
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        nav.refresh_pending_projects()
        seen = []
        nav.openProjectRequested.connect(seen.append)
        nav._on_pending_item_clicked(nav._pending_list.item(0))
        assert seen == ["other-proj"]

    def test_hidden_while_sidebar_collapsed(self, qapp, monkeypatch):
        monkeypatch.setattr(project_nav_module, "list_project_names", lambda: ["other-proj"])
        task_ledger.create_assignment(
            "other-proj", "backend", "/api", "add endpoint", None, None, "claude"
        )
        nav = ProjectNav()
        nav.addTab(_page("a"), "alpha")
        nav.refresh_pending_projects()
        assert nav._pending_list.isHidden() is False
        nav.set_sidebar_collapsed(True, animate=False)
        assert nav._pending_list.isHidden() is True
