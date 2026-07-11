"""Skills vertical slice (SPEC.md §Skills).

General (name/description/instructions editor + path) · Assigned roles
(read-only — editing happens from the Role's Access tab, per SPEC.md
"Relationships"). PROJECT skills get full CRUD; SHIPPED/EXTERNAL are
read-only with an ownership-specific secondary action (Duplicate to
project / Open folder).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from ..commands import CreateSkillCommand, UpdateSkillCommand
from ..models import Ownership, SkillDetail
from ..repositories import skills as skills_repo
from ..widgets.danger_zone import DangerZone
from ..widgets.detail_footer import DetailFooter
from ..widgets.detail_header import DetailHeader
from ..widgets.management_page import ManagementPage


class SkillsPage(ManagementPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Skills",
            filters=("All", "Project", "Shipped", "External"),
            new_button_label="+ New Skill",
            parent=parent,
        )

        self._detail = QWidget(self)
        detail_layout = QVBoxLayout(self._detail)
        self.header = DetailHeader(self._detail)
        detail_layout.addWidget(self.header)

        action_row = QHBoxLayout()
        self.duplicate_btn = theme.secondary_button("Duplicate to project", self._detail)
        self.duplicate_btn.clicked.connect(self._on_duplicate_clicked)
        self.open_folder_btn = theme.secondary_button("Open folder", self._detail)
        self.open_folder_btn.clicked.connect(self._on_open_folder_clicked)
        action_row.addWidget(self.duplicate_btn)
        action_row.addWidget(self.open_folder_btn)
        action_row.addStretch(1)
        detail_layout.addLayout(action_row)

        self.tabs = QTabWidget(self._detail)
        detail_layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_roles_tab()

        self.danger_zone = DangerZone(self._detail)
        self.danger_zone.delete_requested.connect(self._on_delete_confirmed)
        detail_layout.addWidget(self.danger_zone)

        self.footer = DetailFooter(self._detail)
        self.footer.save_clicked.connect(self._on_save_clicked)
        self.footer.discard_clicked.connect(self._on_discard_clicked)
        detail_layout.addWidget(self.footer)

        self.detail_stack.addWidget(self._detail)

        self._current: SkillDetail | None = None
        self._create_mode = False
        self._dirty = False

        self.load_rows = self._load_rows
        self.on_select = self._load_detail
        self.on_new = self._start_create
        self.is_dirty = lambda: self._dirty
        self.save = self._save
        self.discard = self._discard

        self._wire_dirty_tracking()

    # ── tabs ──────────────────────────────────────────────────────

    def _build_general_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)
        self.name_edit = QLineEdit()
        self.description_edit = QLineEdit()
        self.instructions_edit = QPlainTextEdit()
        self.instructions_edit.setMinimumHeight(200)
        self.path_label = QLabel("")
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Instructions", self.instructions_edit)
        form.addRow("Path", self.path_label)
        self.tabs.addTab(tab, "General")

    def _build_roles_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Assigned to (read-only — edit from the role's Access tab):"))
        self.roles_list = QListWidget()
        layout.addWidget(self.roles_list)
        self.manage_roles_btn = theme.secondary_button("Manage roles", tab)
        self.manage_roles_btn.clicked.connect(lambda: self.manage_roles_requested())
        layout.addWidget(self.manage_roles_btn)
        self.tabs.addTab(tab, "Assigned roles")

    def manage_roles_requested(self) -> None:
        """Reassigned by the window shell to jump to the Roles page — a
        hook-attribute like `load_rows`/`on_select`, not a Qt signal, so the
        page stays importable/testable without a shell present."""

    def _wire_dirty_tracking(self) -> None:
        def mark_dirty(*_a: object) -> None:
            if self._current is not None or self._create_mode:
                self._dirty = True
                self.footer.set_dirty(True)

        self.name_edit.textChanged.connect(mark_dirty)
        self.description_edit.textChanged.connect(mark_dirty)
        self.instructions_edit.textChanged.connect(mark_dirty)

    # ── data plumbing ─────────────────────────────────────────────

    def _load_rows(self) -> list[tuple[str, str]]:
        return [
            (
                s.name,
                f"{s.name}  ·  {s.ownership.value}"
                + (f" — {s.description}" if s.description else ""),
            )
            for s in skills_repo.list()
        ]

    def _load_detail(self, entity_id: str) -> None:
        self._create_mode = False
        self._current = skills_repo.get(entity_id)
        d = self._current
        writable = d.capabilities.can_update

        self.header.set_entity(d.name, d.name, d.ownership)
        self.name_edit.setText(d.name)
        self.name_edit.setEnabled(False)  # name immutable after create
        self.description_edit.setText(d.description)
        self.description_edit.setEnabled(writable)
        self.instructions_edit.setPlainText(d.instructions)
        self.instructions_edit.setReadOnly(not writable)
        self.path_label.setText(d.path)

        self.roles_list.clear()
        self.roles_list.addItems(list(d.assigned_roles) or ["(ยังไม่มี role ไหนอ้างถึง skill นี้)"])

        self.duplicate_btn.setVisible(d.ownership is Ownership.SHIPPED)
        self.open_folder_btn.setVisible(d.ownership is Ownership.EXTERNAL)

        self.danger_zone.set_plan(skills_repo.delete_plan(d.name) if writable else None)

        self.footer.set_create_mode(False)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)

    def _start_create(self) -> None:
        self._create_mode = True
        self._current = None
        self.header.set_entity("New Skill", "", Ownership.PROJECT)
        self.name_edit.clear()
        self.name_edit.setEnabled(True)
        self.description_edit.clear()
        self.description_edit.setEnabled(True)
        self.instructions_edit.clear()
        self.instructions_edit.setReadOnly(False)
        self.path_label.setText("")
        self.roles_list.clear()
        self.duplicate_btn.setVisible(False)
        self.open_folder_btn.setVisible(False)
        self.danger_zone.set_plan(None)

        self.footer.set_create_mode(True)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)
        self.name_edit.setFocus()

    def _save(self) -> bool:
        if self._create_mode:
            name = self.name_edit.text().strip().lower()
            result = skills_repo.create(
                CreateSkillCommand(
                    name=name,
                    description=self.description_edit.text().strip(),
                    instructions=self.instructions_edit.toPlainText(),
                )
            )
        else:
            assert self._current is not None
            result = skills_repo.update(
                self._current.name,
                UpdateSkillCommand(
                    description=self.description_edit.text().strip(),
                    instructions=self.instructions_edit.toPlainText(),
                ),
            )

        if not result.ok:
            self._show_error(result.message)
            return False

        self._dirty = False
        self.footer.set_dirty(False)
        self.refresh()
        if result.entity_id:
            self.select(result.entity_id)
        return True

    def _discard(self) -> None:
        if self._current is not None:
            self._load_detail(self._current.name)
        else:
            self.show_empty()
        self._dirty = False
        self.footer.set_dirty(False)

    def _on_save_clicked(self) -> None:
        self._save()

    def _on_discard_clicked(self) -> None:
        self._discard()

    def _on_duplicate_clicked(self) -> None:
        if self._current is None:
            return
        result = skills_repo.duplicate_to_project(self._current.name)
        if not result.ok:
            self._show_error(result.message)
            return
        self.refresh()
        if result.entity_id:
            self.select(result.entity_id)

    def _on_open_folder_clicked(self) -> None:
        if self._current is None:
            return
        folder = Path(self._current.path).parent
        if not folder.is_dir():
            return
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(folder)], check=False)

    def _on_delete_confirmed(self, version: str) -> None:
        if self._current is None:
            return
        result = skills_repo.delete(self._current.name, version)
        if not result.ok:
            self._show_error(result.message)
            return
        self._current = None
        self.refresh()
        self.show_empty()

    def _show_error(self, message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox

        box = theme.themed_message_box(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.entity_label)
        box.setText(message or "เกิดข้อผิดพลาด")
        box.exec()
