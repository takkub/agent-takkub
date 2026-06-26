"""ProjectNav: the sidebar-list + stacked-content replacement for the old top
QTabWidget. Verifies the QTabWidget-compatible API stays in lockstep (row ==
stack index) so MainWindow's ~dozen call sites keep working unchanged.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from agent_takkub.project_nav import ProjectNav


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
