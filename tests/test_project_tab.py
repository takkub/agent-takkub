"""project_tab.py — tab-strip status color + live-retheme (2026-09-08 design
review). Explorer ownership has its own file (test_project_tab_explorer.py);
this one covers what's left: the per-tab status-dot color resolution and the
`retheme()` hook MainWindow calls on a live theme switch.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import cockpit_theme, project_tab
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
    from agent_takkub import project_explorer as pe

    monkeypatch.setattr(pe, "project_roots", lambda name: {"main": tmp_path})


@pytest.fixture
def _restore_dark_variant():
    yield
    cockpit_theme.apply_variant("dark")


class TestTabStatusColor:
    """`_tab_status_color()` resolves `cockpit_theme` attribute NAMES live on
    every call — a dict of already-resolved color strings built once at
    import time would freeze whichever variant happened to be bound then
    (the same stale-snapshot bug `task_dock._STATUS_GLYPH`'s own comment
    documents), silently going stale on a live theme switch."""

    def test_known_states_map_to_distinct_colors(self) -> None:
        working = project_tab._tab_status_color("working")
        done = project_tab._tab_status_color("done")
        default = project_tab._tab_status_color(None)
        assert len({working, done, default}) == 3

    def test_unknown_state_falls_back_without_raising(self) -> None:
        assert project_tab._tab_status_color("bogus") == cockpit_theme.TEXT_FAINT

    def test_color_reads_the_current_theme_variant_live(self, _restore_dark_variant) -> None:
        cockpit_theme.apply_variant("dark")
        dark_color = project_tab._tab_status_color("working")
        assert dark_color == cockpit_theme.DARK_TOKENS["STATE_WARN_BRIGHT"]

        cockpit_theme.apply_variant("light")
        light_color = project_tab._tab_status_color("working")
        assert light_color == cockpit_theme.LIGHT_TOKENS["STATE_WARN_BRIGHT"]
        assert light_color != dark_color


class TestProjectTabRetheme:
    def test_retheme_reapplies_the_pane_tab_strip_qss(self, qapp, _restore_dark_variant) -> None:
        """2026-09-08 design review, live-retheme fix — `_pane_tabs_qss()`
        reads live tokens already, but `setStyleSheet()` was only ever
        called once, at construction; `retheme()` must re-apply it after a
        live `apply_variant()` switch instead of waiting for a restart."""
        tab = ProjectTab("proj-tab-retheme-test")
        try:
            cockpit_theme.apply_variant("light")
            tab.retheme()
            assert cockpit_theme.LIGHT_TOKENS["GROUND_SIDEBAR"] in tab.pane_tabs.styleSheet()
        finally:
            tab.deleteLater()
