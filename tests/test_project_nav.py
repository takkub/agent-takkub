"""ProjectNav: the sidebar-list + stacked-content replacement for the old top
QTabWidget. Verifies the QTabWidget-compatible API stays in lockstep (row ==
stack index) so MainWindow's ~dozen call sites keep working unchanged.
"""

from __future__ import annotations

import pathlib

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from agent_takkub import cockpit_theme, task_ledger
from agent_takkub import project_nav as project_nav_module
from agent_takkub.git_changes_service import GitChangesService
from agent_takkub.project_file_index import GitStatusService
from agent_takkub.project_nav import ProjectNav
from agent_takkub.project_tab import ProjectTab

from ._qt_timer_leak_guard import stop_timers_after


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


@pytest.fixture(autouse=True)
def _stop_nav_tab_timers(monkeypatch):
    # ProjectNav._pending_timer and ProjectTab._tab_status_timer both start
    # unconditionally in __init__ (#344) — every construction in this file
    # otherwise leaves one running for the rest of the pytest session. A
    # ProjectTab with a real ProjectExplorer (TestExplorerEmbedding below)
    # also spins up GitStatusService/GitChangesService's own QTimers.
    finalize_nav = stop_timers_after(monkeypatch, ProjectNav, "_pending_timer")
    finalize_tab = stop_timers_after(monkeypatch, ProjectTab, "_tab_status_timer")
    finalize_git = stop_timers_after(monkeypatch, GitStatusService, "_timer")
    finalize_changes = stop_timers_after(monkeypatch, GitChangesService, "_timer")
    yield
    finalize_nav()
    finalize_tab()
    for finalize in (finalize_git, finalize_changes):
        try:
            finalize()
        except RuntimeError:
            pass  # explorer (and its child git service/QTimer) already GC'd


@pytest.fixture(autouse=True)
def _stub_project_roots(monkeypatch, tmp_path: pathlib.Path) -> None:
    """Every ProjectTab constructed in this file builds a real
    ProjectExplorer, which reads project_roots() — stub it so no test needs
    a real projects.json / DATA_HOME (same fixture as test_project_tab_explorer.py)."""
    from agent_takkub import project_explorer as pe

    monkeypatch.setattr(pe, "project_roots", lambda name: {"main": tmp_path})


@pytest.fixture(autouse=True)
def isolated_nav_qsettings(monkeypatch, tmp_path: pathlib.Path) -> str:
    """Redirect ProjectNav's `QSettings("agent-takkub", "cockpit")` calls
    (every `ProjectNav()` construction makes one, for the explorer's
    per-project expanded/collapsed flag) to a throwaway per-test INI file —
    without this, every test in this file would read/write the real
    machine's registry/INI store under the same org/app MainWindow uses for
    window geometry. Autouse: this isn't opt-in per test."""
    ini_path = str(tmp_path / "cockpit_settings.ini")

    def _factory(*_args, **_kwargs):
        return QSettings(ini_path, QSettings.Format.IniFormat)

    monkeypatch.setattr(project_nav_module, "QSettings", _factory)
    return ini_path


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


class TestTunnelIndicator:
    """Top-left sidebar dot surfacing tunnel liveness (separate from the
    bottom-status-bar 🌐 Remote chip — see status_header._refresh_tunnel_indicator)."""

    def test_default_state_is_neutral(self, qapp):
        nav = ProjectNav()
        assert "not enabled" in nav._tunnel_indicator.toolTip()

    def test_running_state_paints_ok_color(self, qapp):
        nav = ProjectNav()
        nav.set_tunnel_status("running", "Tunnel: running")
        assert cockpit_theme.STATE_OK in nav._tunnel_indicator.styleSheet()
        assert nav._tunnel_indicator.toolTip() == "Tunnel: running"

    def test_error_state_paints_error_color(self, qapp):
        nav = ProjectNav()
        nav.set_tunnel_status("error", "Tunnel: boom")
        assert cockpit_theme.STATE_ERROR in nav._tunnel_indicator.styleSheet()
        assert nav._tunnel_indicator.toolTip() == "Tunnel: boom"

    def test_off_state_paints_neutral_color(self, qapp):
        nav = ProjectNav()
        nav.set_tunnel_status("running", "x")  # flip away from default first
        nav.set_tunnel_status("off", "Tunnel: off")
        assert cockpit_theme.TEXT_FAINT in nav._tunnel_indicator.styleSheet()

    def test_unknown_state_falls_back_to_neutral_without_raising(self, qapp):
        nav = ProjectNav()
        nav.set_tunnel_status("bogus", "?")  # must not raise
        assert cockpit_theme.TEXT_FAINT in nav._tunnel_indicator.styleSheet()


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


class TestExplorerEmbedding:
    """#365 feedback 2026-08-23: the file tree now lives directly under its
    own project's sidebar card — not a separate QSplitter panel between the
    sidebar and the pane area (that lasted one release; see project_nav.py's
    module docstring). Only the currently-selected row ever shows a tree,
    and a chevron on that row is the one manual toggle."""

    def test_explorer_embedded_and_shown_for_first_selected_project(self, qapp) -> None:
        tab = ProjectTab("proj-embed-a")
        nav = ProjectNav()
        nav.addTab(tab, "proj-embed-a")
        row = nav._row_widget(0)

        assert row.has_explorer() is True
        assert tab.explorer.parent() is row._explorer_container
        assert tab.explorer.isHidden() is False
        assert row._chevron.isHidden() is False

    def test_switching_project_hides_previous_row_tree_and_shows_new(self, qapp) -> None:
        tab_a = ProjectTab("proj-embed-b")
        tab_b = ProjectTab("proj-embed-c")
        nav = ProjectNav()
        nav.addTab(tab_a, "proj-embed-b")
        nav.addTab(tab_b, "proj-embed-c")
        row_a, row_b = nav._row_widget(0), nav._row_widget(1)

        # row 0 auto-selected on first add; row 1's tree/chevron stay hidden
        # until it becomes the active project.
        assert tab_a.explorer.isHidden() is False
        assert row_b._chevron.isHidden() is True

        nav.setCurrentIndex(1)

        assert tab_a.explorer.isHidden() is True
        assert row_a._chevron.isHidden() is True
        assert tab_b.explorer.isHidden() is False
        assert row_b._chevron.isHidden() is False

    def test_chevron_click_collapses_and_expands_the_tree(self, qapp) -> None:
        tab = ProjectTab("proj-embed-d")
        nav = ProjectNav()
        nav.addTab(tab, "proj-embed-d")
        row = nav._row_widget(0)
        assert tab.explorer.isHidden() is False

        row._chevron.click()
        assert tab.explorer.isHidden() is True

        row._chevron.click()
        assert tab.explorer.isHidden() is False

    def test_rail_collapse_hides_chevron_and_tree_regardless_of_expanded_flag(self, qapp) -> None:
        tab = ProjectTab("proj-embed-rail")
        nav = ProjectNav()
        nav.addTab(tab, "proj-embed-rail")
        row = nav._row_widget(0)
        assert tab.explorer.isHidden() is False

        nav.set_sidebar_collapsed(True, animate=False)
        assert row._chevron.isHidden() is True
        assert tab.explorer.isHidden() is True

        nav.set_sidebar_collapsed(False, animate=False)
        assert row._chevron.isHidden() is False
        assert tab.explorer.isHidden() is False  # remembered expanded, not lost

    def test_remove_tab_reparents_explorer_back_to_project_tab(self, qapp) -> None:
        tab = ProjectTab("proj-embed-e")
        nav = ProjectNav()
        nav.addTab(tab, "proj-embed-e")

        nav.removeTab(0)

        assert tab.explorer.parent() is tab

    def test_project_with_no_explorer_gets_no_chevron(self, qapp) -> None:
        nav = ProjectNav()
        nav.addTab(_page("plain"), "plain")  # bare QLabel has no .explorer
        row = nav._row_widget(0)

        assert row.has_explorer() is False
        assert row._chevron.isHidden() is True

    def test_signals_still_reach_project_tab_after_embedding(self, qapp) -> None:
        tab = ProjectTab("proj-embed-f")
        nav = ProjectNav()
        nav.addTab(tab, "proj-embed-f")
        received: list[tuple[str, str]] = []
        tab.openFileRequested.connect(lambda proj, path: received.append((proj, path)))

        tab.explorer.fileActivated.emit("/abs/path/x.py")

        assert received == [("proj-embed-f", "/abs/path/x.py")]


class TestExplorerCollapsePersistence:
    """The chevron's expanded/collapsed flag persists per-project in
    QSettings — same mechanism/keys ProjectTab used before the explorer
    moved into the sidebar."""

    def test_collapsed_state_persists_across_nav_instances(self, qapp) -> None:
        tab1 = ProjectTab("proj-nav-persist")
        nav1 = ProjectNav()
        nav1.addTab(tab1, "proj-nav-persist")
        nav1._row_widget(0)._chevron.click()  # collapse
        assert tab1.explorer.isHidden() is True

        tab2 = ProjectTab("proj-nav-persist")
        nav2 = ProjectNav()
        nav2.addTab(tab2, "proj-nav-persist")
        assert tab2.explorer.isHidden() is True  # remembered collapsed

    def test_different_projects_do_not_share_collapse_state(self, qapp) -> None:
        tab_a = ProjectTab("proj-nav-x")
        nav_a = ProjectNav()
        nav_a.addTab(tab_a, "proj-nav-x")
        nav_a._row_widget(0)._chevron.click()  # collapse only proj-nav-x

        tab_b = ProjectTab("proj-nav-y")
        nav_b = ProjectNav()
        nav_b.addTab(tab_b, "proj-nav-y")
        assert tab_b.explorer.isHidden() is False


class TestExplorerFillsSidebar:
    """User feedback 2026-08-23 (post-#365): the tree used to be a fixed
    260px slab, leaving a dead band under it on any tall window. It now
    fills the list viewport down to the bottom edge — header rows of every
    project subtracted — and only scrolls internally once the viewport is
    too short to give it even `_EXPLORER_MIN_H`."""

    @staticmethod
    def _pump(qapp) -> None:
        # _fit_explorer is coalesced onto a singleShot(0) — drain it.
        for _ in range(3):
            qapp.processEvents()

    def test_tree_height_tracks_list_viewport(self, qapp) -> None:
        tab = ProjectTab("proj-fill-a")
        nav = ProjectNav()
        nav.resize(400, 900)
        nav.show()
        nav.addTab(tab, "proj-fill-a")
        self._pump(qapp)
        row = nav._row_widget(0)

        viewport_h = nav._list.viewport().height()
        expected = viewport_h - row.header_height() - nav._list.spacing() * 2
        assert tab.explorer.height() == max(project_nav_module._EXPLORER_MIN_H, expected)
        assert tab.explorer.height() > 260  # no longer the old fixed slab
        # the list item is flush with the viewport: row sizeHint == viewport
        assert nav._list.item(0).sizeHint().height() <= viewport_h
        assert nav._list.item(0).sizeHint().height() >= viewport_h - nav._list.spacing() * 2

        # shrink the window → the tree follows, still floored at the min
        nav.resize(400, 500)
        self._pump(qapp)
        h2 = tab.explorer.height()
        assert project_nav_module._EXPLORER_MIN_H <= h2 < viewport_h
        nav.close()

    def test_other_rows_headers_are_subtracted(self, qapp) -> None:
        tab_a = ProjectTab("proj-fill-b")
        tab_b = ProjectTab("proj-fill-c")
        nav = ProjectNav()
        nav.resize(400, 900)
        nav.show()
        nav.addTab(tab_a, "proj-fill-b")
        self._pump(qapp)
        h_one = tab_a.explorer.height()
        nav.addTab(tab_b, "proj-fill-c")
        self._pump(qapp)
        h_two = tab_a.explorer.height()
        # second row's header eats into the tree's share
        assert h_two < h_one
        assert h_one - h_two >= nav._row_widget(1).header_height() - 2
        nav.close()

    def test_floor_when_viewport_too_short(self, qapp) -> None:
        tab = ProjectTab("proj-fill-d")
        nav = ProjectNav()
        nav.resize(400, 160)
        nav.show()
        nav.addTab(tab, "proj-fill-d")
        self._pump(qapp)
        assert tab.explorer.height() == project_nav_module._EXPLORER_MIN_H
        nav.close()
