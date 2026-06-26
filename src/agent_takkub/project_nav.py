"""ProjectNav: a left sidebar project list + right stacked content area.

Drop-in replacement for the old top `QTabWidget` that hosted one ProjectTab
per project. The redesign (2026-06-26) moves project selection from a top tab
strip to a **left sidebar list**; the selected project's panes fill the right.
This is presentation-only — it exposes the slice of the `QTabWidget` API that
MainWindow already used (`addTab`/`insertTab`/`removeTab`/`widget`/`count`/
`currentWidget`/`currentIndex`/`setCurrentIndex`/`indexOf` + a `currentChanged`
signal) so the ~dozen call sites keep working unchanged. Tab-bar-only concerns
(the "+" pseudo-tab, per-tab close button, context menu) become sidebar idioms
surfaced as dedicated signals: `addRequested`, `closeRequested(index)`,
`contextMenuRequested(index, globalPos)`.

Row index == stacked-widget index == list row, kept in lockstep at all times.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .token_meter import usage_color

_SIDEBAR_QSS = """
#projectSidebar {
    background: #0e0e10;
    border-right: 1px solid #27272a;
}
#projectSidebarHeader {
    color: #52525b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 14px 16px 8px 16px;
}
QListWidget#projectList {
    background: transparent;
    border: none;
    outline: 0;
    padding: 2px 8px;
}
QListWidget#projectList::item {
    border-radius: 8px;
    margin: 2px 0;
    padding: 0;
}
QListWidget#projectList::item:hover {
    background: #18181b;
}
QListWidget#projectList::item:selected {
    background: #1e1b2e;
}
#addProjectBtn {
    background: transparent;
    color: #818cf8;
    border: 1px dashed #3f3f46;
    border-radius: 8px;
    padding: 9px;
    margin: 8px 12px 12px 12px;
    font-size: 12px;
    font-weight: 600;
}
#addProjectBtn:hover {
    background: #18181b;
    border-color: #6366f1;
    color: #a5b4fc;
}
"""


class _ProjectRow(QWidget):
    """Custom sidebar row: project name + a right-aligned usage % badge.

    Background stays transparent so the QListWidget's :selected / :hover
    highlight shows through behind it.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(8)

        self._accent = QLabel("●")
        self._accent.setStyleSheet("color: #6366f1; font-size: 9px;")
        self._accent.setVisible(False)  # shown only on the selected row

        self._name = QLabel(name)
        self._name.setStyleSheet("color: #d4d4d8; font-size: 13px; font-weight: 600;")

        self._badge = QLabel("")
        self._badge.setStyleSheet(
            "color: #52525b; font-size: 11px; font-variant-numeric: tabular-nums;"
        )

        lay.addWidget(self._accent)
        lay.addWidget(self._name, 1)
        lay.addWidget(self._badge)

    def set_name(self, name: str) -> None:
        self._name.setText(name)

    def set_selected(self, selected: bool) -> None:
        self._accent.setVisible(selected)
        color = "#ffffff" if selected else "#d4d4d8"
        self._name.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 600;")

    def set_usage(self, ratio: float | None) -> None:
        if ratio is None:
            self._badge.setText("")
            return
        self._badge.setText(f"{int(ratio * 100)}%")
        self._badge.setStyleSheet(
            f"color: {usage_color(ratio)}; font-size: 11px; font-variant-numeric: tabular-nums;"
        )


class ProjectNav(QWidget):
    """Sidebar project list + stacked content. QTabWidget-compatible subset."""

    currentChanged = pyqtSignal(int)
    addRequested = pyqtSignal()
    closeRequested = pyqtSignal(int)
    contextMenuRequested = pyqtSignal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── left sidebar ────────────────────────────────────────────
        sidebar = QWidget(self)
        sidebar.setObjectName("projectSidebar")
        sidebar.setFixedWidth(212)
        sidebar.setStyleSheet(_SIDEBAR_QSS)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)

        header = QLabel("PROJECTS")
        header.setObjectName("projectSidebarHeader")
        sb.addWidget(header)

        self._list = QListWidget(sidebar)
        self._list.setObjectName("projectList")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        sb.addWidget(self._list, 1)

        self._add_btn = QPushButton("+   New project", sidebar)
        self._add_btn.setObjectName("addProjectBtn")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(self.addRequested.emit)
        sb.addWidget(self._add_btn)

        # ── right content ───────────────────────────────────────────
        self._stack = QStackedWidget(self)

        root.addWidget(sidebar)
        root.addWidget(self._stack, 1)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _row_widget(self, index: int) -> _ProjectRow | None:
        item = self._list.item(index)
        if item is None:
            return None
        w = self._list.itemWidget(item)
        return w if isinstance(w, _ProjectRow) else None

    def _on_row_changed(self, row: int) -> None:
        # Keep the stack in lockstep and repaint selection accents.
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is not None:
                rw.set_selected(i == row)
        self.currentChanged.emit(row)

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        index = self._list.row(item)
        self.contextMenuRequested.emit(index, self._list.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # QTabWidget-compatible API
    # ------------------------------------------------------------------
    def count(self) -> int:
        return self._stack.count()

    def widget(self, index: int) -> QWidget | None:
        return self._stack.widget(index)

    def currentWidget(self) -> QWidget | None:
        return self._stack.currentWidget()

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def indexOf(self, w: QWidget) -> int:
        return self._stack.indexOf(w)

    def setCurrentIndex(self, index: int) -> None:
        # Drive selection through the list so the accent + currentChanged path
        # runs exactly once. _on_row_changed mirrors it onto the stack.
        if 0 <= index < self._list.count():
            self._list.setCurrentRow(index)

    def addTab(self, w: QWidget, label: str) -> int:
        index = self._stack.addWidget(w)
        self._insert_list_row(index, label)
        return index

    def insertTab(self, index: int, w: QWidget, label: str) -> int:
        self._stack.insertWidget(index, w)
        self._insert_list_row(index, label)
        return index

    def removeTab(self, index: int) -> None:
        w = self._stack.widget(index)
        if w is not None:
            self._stack.removeWidget(w)
        item = self._list.takeItem(index)
        if item is not None:
            del item

    def setTabText(self, index: int, text: str) -> None:
        rw = self._row_widget(index)
        if rw is not None:
            rw.set_name(text)

    def setTabToolTip(self, index: int, text: str) -> None:
        item = self._list.item(index)
        if item is not None:
            item.setToolTip(text)

    def set_usage(self, index: int, ratio: float | None) -> None:
        """Sidebar-specific: show a project's peak context-usage %. (Replaces
        the old per-tab `setTabText(... · NN%)` badge.)"""
        rw = self._row_widget(index)
        if rw is not None:
            rw.set_usage(ratio)

    # ------------------------------------------------------------------
    def _insert_list_row(self, index: int, label: str) -> None:
        item = QListWidgetItem()
        row = _ProjectRow(label)
        item.setSizeHint(row.sizeHint())
        self._list.insertItem(index, item)
        self._list.setItemWidget(item, row)
