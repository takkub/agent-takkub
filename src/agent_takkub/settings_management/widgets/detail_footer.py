"""Detail-pane footer — Discard (left) / Save changes (right) + unsaved dot.

SPEC.md "Save model": staged draft per entity, footer owns Save/Discard —
never a window-level global Save button (that's what the legacy window does
and is exactly the ambiguity this redesign removes)."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ... import cockpit_theme as theme


class DetailFooter(QWidget):
    save_clicked = pyqtSignal()
    discard_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, create_mode: bool = False) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        self._unsaved = QLabel("", self)
        self._unsaved.setObjectName("unsavedLabel")
        self._unsaved.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self._unsaved)
        layout.addStretch(1)

        self._discard_btn = theme.secondary_button("Discard", self)
        self._discard_btn.clicked.connect(self.discard_clicked.emit)
        layout.addWidget(self._discard_btn)

        self._save_btn = theme.gold_button("Create" if create_mode else "Save changes", self)
        self._save_btn.clicked.connect(self.save_clicked.emit)
        layout.addWidget(self._save_btn)

        self.set_dirty(False)

    def set_dirty(self, dirty: bool) -> None:
        self._unsaved.setText("● Unsaved changes" if dirty else "No unsaved changes")
        self._save_btn.setEnabled(dirty)

    def set_save_enabled(self, enabled: bool) -> None:
        self._save_btn.setEnabled(enabled)

    def set_create_mode(self, create_mode: bool) -> None:
        self._save_btn.setText("Create" if create_mode else "Save changes")
        self._discard_btn.setText("Cancel" if create_mode else "Discard")
