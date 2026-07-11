"""Reusable list-detail shell — the ONE pattern every entity page uses
(SPEC.md "Unified CRUD pattern"). Owns the search/filter/list on the left,
a ``QStackedWidget`` (empty-state / detail) on the right, and the
draft-guard dialog when switching selection while dirty.

A concrete page (e.g. ``pages/roles_page.py``) wires four callables and
never touches ``EntityList`` layout/selection plumbing directly:

  ``load_rows() -> list[tuple[str, str]]``       refresh the left list
  ``on_select(entity_id) -> None``                 populate detail for id
  ``on_new() -> None``                             enter create mode
  ``is_dirty() -> bool`` / ``save() -> bool`` / ``discard() -> None``
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from .entity_list import EntityList


class ManagementPage(QWidget):
    def __init__(
        self,
        entity_label: str,
        filters: tuple[str, ...] = ("All",),
        new_button_label: str = "+ New",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entity_label = entity_label

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.title_label = QLabel(entity_label, self)
        self.title_label.setObjectName("contentTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        self.list = EntityList(filters, new_button_label, self)
        self.list.setMinimumWidth(280)
        body.addWidget(self.list, 0)

        self.detail_stack = QStackedWidget(self)
        body.addWidget(self.detail_stack, 1)

        self.empty_placeholder = QLabel(
            "Select an item from the list to view or edit its settings.", self
        )
        self.empty_placeholder.setWordWrap(True)
        self.empty_placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED}; padding: 24px;")
        self.detail_stack.addWidget(self.empty_placeholder)
        self.detail_stack.setCurrentWidget(self.empty_placeholder)

        self._current_id: str | None = None
        self._all_rows: list[tuple[str, str]] = []
        self._search_text = ""
        self._active_filter = filters[0] if filters else "All"

        # Hooks — the concrete page overwrites these.
        self.load_rows: Callable[[], list[tuple[str, str]]] = lambda: []
        self.on_select: Callable[[str], None] = lambda _entity_id: None
        self.on_new: Callable[[], None] = lambda: None
        self.is_dirty: Callable[[], bool] = lambda: False
        self.save: Callable[[], bool] = lambda: True
        self.discard: Callable[[], None] = lambda: None

        self.list.selection_changed.connect(self._on_selection_changed)
        self.list.new_clicked.connect(self._on_new_clicked)
        self.list.search_changed.connect(self._on_search_changed)
        self.list.filter_changed.connect(self._on_filter_changed)

    def refresh(self) -> None:
        self._all_rows = self.load_rows()
        self._apply_filter()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    def _on_filter_changed(self, name: str) -> None:
        self._active_filter = name
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self._search_text.strip().lower()
        chip = self._active_filter.strip().lower()
        rows = self._all_rows
        if chip and chip != "all":
            rows = [row for row in rows if chip in row[1].lower()]
        if query:
            rows = [row for row in rows if query in row[0].lower() or query in row[1].lower()]
        self.list.set_items(rows)

    def show_empty(self) -> None:
        self._current_id = None
        self.detail_stack.setCurrentWidget(self.empty_placeholder)

    def select(self, entity_id: str) -> None:
        """Programmatic selection (e.g. right after a successful create)."""
        self.list.select(entity_id)

    def _ask_draft_guard(self) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText("มีการแก้ไขที่ยังไม่ได้บันทึก — Save ก่อนสลับ หรือทิ้งการแก้ไข?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec()

    def _on_selection_changed(self, entity_id: str) -> None:
        if entity_id == (self._current_id or ""):
            return
        if self.is_dirty():
            choice = self._ask_draft_guard()
            if choice == QMessageBox.StandardButton.Save:
                if not self.save():
                    self.list.select(self._current_id)
                    return
            elif choice == QMessageBox.StandardButton.Discard:
                self.discard()
            else:  # Cancel = "Keep editing"
                self.list.select(self._current_id)
                return

        self._current_id = entity_id or None
        if self._current_id:
            self.on_select(self._current_id)
        else:
            self.show_empty()

    def _on_new_clicked(self) -> None:
        if self.is_dirty():
            choice = self._ask_draft_guard()
            if choice == QMessageBox.StandardButton.Save:
                if not self.save():
                    return
            elif choice == QMessageBox.StandardButton.Cancel:
                return
            else:
                self.discard()
        self.list.select(None)
        self._current_id = None
        self.on_new()
