"""Danger zone — bottom of Advanced tab. Delete button + effects/blockers
confirm dialog (SPEC.md "Delete": confirm shows effects, not a generic
"Are you sure?"; failure shows the service's error and the item is not
removed from the list before persistence succeeds)."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QMessageBox, QVBoxLayout, QWidget

from ... import cockpit_theme as theme
from ..models import DeletePlan


class DangerZone(QWidget):
    delete_requested = pyqtSignal(str)  # confirmed_plan_version

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)

        title = QLabel("Danger zone", self)
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self._reason = QLabel("", self)
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self._reason)

        self._delete_btn = theme.secondary_button("Delete", self)
        self._delete_btn.setStyleSheet(
            self._delete_btn.styleSheet() + f"QPushButton {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self._delete_btn)

        self._plan: DeletePlan | None = None
        self.set_plan(None)

    def set_plan(self, plan: DeletePlan | None) -> None:
        self._plan = plan
        if plan is None:
            self._delete_btn.setEnabled(False)
            self._reason.setText("")
            return
        if not plan.deletable:
            self._delete_btn.setEnabled(False)
            self._reason.setText(" · ".join(plan.blockers) or "ลบไม่ได้")
        else:
            self._delete_btn.setEnabled(True)
            self._reason.setText("")

    def _on_delete_clicked(self) -> None:
        if self._plan is None or not self._plan.deletable:
            return
        effects = "\n".join(f"• {e}" for e in self._plan.effects)
        box = QMessageBox(self)
        box.setWindowTitle("Delete")
        box.setText(f"ลบ '{self._plan.entity_id}'?\n\nจะเกิดผลดังนี้:\n{effects}")
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(self._plan.version)
