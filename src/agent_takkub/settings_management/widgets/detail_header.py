"""Detail-pane header — display name + immutable id + ownership badge."""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ... import cockpit_theme as theme
from ..models import Ownership
from .source_badge import make_source_badge


class DetailHeader(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._title = QLabel(self)
        self._title.setObjectName("contentTitle")
        row.addWidget(self._title)
        self._badge_slot = QHBoxLayout()
        row.addLayout(self._badge_slot)
        row.addStretch(1)
        outer.addLayout(row)

        self._sub = QLabel(self)
        self._sub.setObjectName("contentSub")
        self._sub.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        outer.addWidget(self._sub)

        self._badge: QWidget | None = None

    def set_entity(self, title: str, entity_id: str, ownership: Ownership) -> None:
        self._title.setText(title)
        self._sub.setText(entity_id)
        if self._badge is not None:
            self._badge_slot.removeWidget(self._badge)
            self._badge.deleteLater()
        self._badge = make_source_badge(ownership, self)
        self._badge_slot.addWidget(self._badge)
