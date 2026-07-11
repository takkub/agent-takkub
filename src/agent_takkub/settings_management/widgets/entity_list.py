"""Search box + filter chips + QListWidget — the left pane of every entity
page (SPEC.md "List pane")."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme


class EntityList(QWidget):
    """A generic filterable list. Rows are (entity_id, display_text) pairs;
    the page owning this widget decides what "matches a filter" means by
    calling :meth:`set_items` with pre-filtered rows whenever the search
    text or active filter chip changes."""

    search_changed = pyqtSignal(str)
    filter_changed = pyqtSignal(str)
    selection_changed = pyqtSignal(str)  # entity_id, "" when cleared
    new_clicked = pyqtSignal()

    def __init__(
        self,
        filters: tuple[str, ...] = ("All",),
        new_button_label: str = "+ New",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search...")
        self._search.textChanged.connect(self.search_changed.emit)
        layout.addWidget(self._search)

        if len(filters) > 1:
            chip_row = QHBoxLayout()
            self._filter_group = QButtonGroup(self)
            self._filter_group.setExclusive(True)
            for i, name in enumerate(filters):
                btn = QPushButton(name, self)
                btn.setCheckable(True)
                btn.setChecked(i == 0)
                btn.setObjectName("secondaryButton")
                btn.clicked.connect(lambda _checked, n=name: self.filter_changed.emit(n))
                self._filter_group.addButton(btn)
                chip_row.addWidget(btn)
            chip_row.addStretch(1)
            layout.addLayout(chip_row)
        else:
            self._filter_group = None

        self._list = QListWidget(self)
        # Long descriptions ("cockpit-ui-style · project — The single design
        # system…") used to spill past the list width and force a horizontal
        # scrollbar that clipped trailing content like a BLOCKED tag (critic
        # R2) — elide instead and keep the full text one hover away.
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self._list, 1)

        self._new_btn = theme.secondary_button(new_button_label, self)
        self._new_btn.clicked.connect(self.new_clicked.emit)
        layout.addWidget(self._new_btn)

    def set_items(self, rows: list[tuple[str, str]]) -> None:
        """Replace list contents. ``rows`` = [(entity_id, display_text), ...]."""
        previous = self.current_entity_id()
        self._list.blockSignals(True)
        self._list.clear()
        for entity_id, text in rows:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, entity_id)
            item.setToolTip(text)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self.select(previous)

    def select(self, entity_id: str | None) -> None:
        if not entity_id:
            self._list.setCurrentRow(-1)
            return
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == entity_id:
                self._list.setCurrentItem(item)
                return
        self._list.setCurrentRow(-1)

    def current_entity_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_current_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self.selection_changed.emit(current.data(Qt.ItemDataRole.UserRole) if current else "")
