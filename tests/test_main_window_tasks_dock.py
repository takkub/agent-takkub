"""A8-polish item 3: Task List dock chrome — no duplicate titlebar, never floats.

`MainWindow._configure_tasks_dock_chrome` is a staticmethod factored out of
`MainWindow.__init__` specifically so it's testable against a bare
`QDockWidget` without booting the full cockpit (orchestrator, CLI server,
Lead pane) that a real `MainWindow()` needs.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow

from agent_takkub.main_window import MainWindow


def test_no_features_at_all_no_closable_movable_or_floatable() -> None:
    """DockWidgetMovable alone (the previous fix) still let the dock be
    dragged out into a floating top-level window (A8-regression item 2) —
    the dock never needs to move between areas (it's the only widget in its
    one allowed area), so all features are dropped."""
    dock = QDockWidget("Task List")
    MainWindow._configure_tasks_dock_chrome(dock)
    features = dock.features()
    assert features == QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
    assert not (features & QDockWidget.DockWidgetFeature.DockWidgetMovable)
    assert not (features & QDockWidget.DockWidgetFeature.DockWidgetClosable)
    assert not (features & QDockWidget.DockWidgetFeature.DockWidgetFloatable)


def test_titlebar_widget_is_blank_zero_height() -> None:
    dock = QDockWidget("Task List")
    MainWindow._configure_tasks_dock_chrome(dock)
    titlebar = dock.titleBarWidget()
    assert titlebar is not None
    assert titlebar.maximumHeight() == 0


def test_docked_in_a_main_window_is_never_floating() -> None:
    """Reproduces the real init order in MainWindow.__init__: configure
    chrome BEFORE addDockWidget() actually docks it into the right area."""
    mw = QMainWindow()
    dock = QDockWidget("Task List", mw)
    dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
    MainWindow._configure_tasks_dock_chrome(dock)
    dock.hide()
    mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    assert dock.isFloating() is False
