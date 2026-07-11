"""Roles vertical slice — SPEC.md's proving-ground page.

General (name/label/color/instructions) · Access (provider + skills +
MCP/plugin tri-state) · Advanced (grid placement + Danger zone). Built-in
roles keep General read-only but Access stays editable (own-overrides model
per codex proposal's "Built-in role affordance").
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QLocale
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from ... import provider_config
from ...provider_spec import PROVIDER_REGISTRY
from ..commands import CreateRoleCommand, RoleAccessDraft, RoleGeneralDraft, UpdateRoleCommand
from ..models import Ownership, RoleDetail
from ..repositories import roles as roles_repo
from ..services import validation
from ..widgets.danger_zone import DangerZone
from ..widgets.detail_footer import DetailFooter
from ..widgets.detail_header import DetailHeader
from ..widgets.management_page import ManagementPage


def _checked_names(lst: QListWidget) -> list[str]:
    from PyQt6.QtCore import Qt

    out = []
    for i in range(lst.count()):
        item = lst.item(i)
        if item.checkState() == Qt.CheckState.Checked:
            out.append(item.text())
    return out


class RolesPage(ManagementPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Roles",
            filters=("All", "Custom", "Built-in"),
            new_button_label="+ New Role",
            parent=parent,
        )

        self._current: RoleDetail | None = None
        self._create_mode = False
        self._access_forced = False

        self._detail = QWidget(self)
        detail_layout = QVBoxLayout(self._detail)
        self.header = DetailHeader(self._detail)
        detail_layout.addWidget(self.header)

        self.tabs = QTabWidget(self._detail)
        detail_layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_access_tab()
        self._build_advanced_tab()

        # Danger zone lives below the tabs (same fixed spot Skills/Plugins/MCP
        # use — SPEC.md consistency: delete must be findable in the same
        # place regardless of entity, not buried inside a tab that only this
        # page happens to have).
        self.danger_zone = DangerZone(self._detail)
        self.danger_zone.delete_requested.connect(self._on_delete_confirmed)
        detail_layout.addWidget(self.danger_zone)

        self.footer = DetailFooter(self._detail)
        self.footer.save_clicked.connect(self._on_save_clicked)
        self.footer.discard_clicked.connect(self._on_discard_clicked)
        detail_layout.addWidget(self.footer)

        self.detail_stack.addWidget(self._detail)

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
        self.label_edit = QLineEdit()
        self.color_edit = QLineEdit()
        self.color_edit.setPlaceholderText("#rrggbb")
        self.instructions_edit = QPlainTextEdit()
        self.instructions_edit.setMinimumHeight(160)
        form.addRow("Role ID", self.name_edit)
        form.addRow("Display name", self.label_edit)
        form.addRow("Color", self.color_edit)
        form.addRow("Instructions", self.instructions_edit)
        self.tabs.addTab(tab, "General")

    def _build_access_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        provider_row = QFormLayout()
        self.provider_combo = QComboBox()
        for name in PROVIDER_REGISTRY:
            self.provider_combo.addItem(name)
        self.provider_note = QLabel("")
        self.provider_note.setWordWrap(True)
        self.provider_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        provider_row.addRow("Provider", self.provider_combo)
        layout.addLayout(provider_row)
        layout.addWidget(self.provider_note)
        self.provider_combo.currentIndexChanged.connect(self._update_provider_note)

        layout.addWidget(QLabel("Skills"))
        self.skills_list = QListWidget()
        layout.addWidget(self.skills_list)

        layout.addWidget(QLabel("MCP Servers"))
        self.mcps_use_defaults = QCheckBox("Use role defaults")
        layout.addWidget(self.mcps_use_defaults)
        self.mcps_list = QListWidget()
        layout.addWidget(self.mcps_list)
        self.mcps_use_defaults.toggled.connect(lambda on: self.mcps_list.setDisabled(on))

        layout.addWidget(QLabel("Plugins"))
        self.plugins_use_defaults = QCheckBox("Use role defaults")
        layout.addWidget(self.plugins_use_defaults)
        self.plugins_list = QListWidget()
        layout.addWidget(self.plugins_list)
        self.plugins_use_defaults.toggled.connect(lambda on: self.plugins_list.setDisabled(on))

        self.tabs.addTab(tab, "Access")

    def _build_advanced_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        # QSpinBox renders digits with the OS locale's native numeral system
        # (Thai on a Thai-locale Windows box) unless pinned to C — same fix
        # as settings_window.py's legacy spin row.
        self.column_spin = QSpinBox()
        self.column_spin.setLocale(QLocale(QLocale.Language.C))
        self.column_spin.setRange(1, 2)
        self.row_spin = QSpinBox()
        self.row_spin.setLocale(QLocale(QLocale.Language.C))
        self.row_spin.setRange(0, 99)
        form.addRow("Column (1=Dev, 2=Support)", self.column_spin)
        form.addRow("Row", self.row_spin)
        layout.addLayout(form)
        layout.addStretch(1)

        self.tabs.addTab(tab, "Advanced")

    def _wire_dirty_tracking(self) -> None:
        def mark_dirty(*_a: object) -> None:
            if self._current is not None or self._create_mode:
                self._dirty = True
                self.footer.set_dirty(True)

        self.name_edit.textChanged.connect(mark_dirty)
        self.label_edit.textChanged.connect(mark_dirty)
        self.color_edit.textChanged.connect(mark_dirty)
        self.instructions_edit.textChanged.connect(mark_dirty)
        self.provider_combo.currentIndexChanged.connect(mark_dirty)
        self.skills_list.itemChanged.connect(mark_dirty)
        self.mcps_list.itemChanged.connect(mark_dirty)
        self.plugins_list.itemChanged.connect(mark_dirty)
        self.mcps_use_defaults.toggled.connect(mark_dirty)
        self.plugins_use_defaults.toggled.connect(mark_dirty)
        self.column_spin.valueChanged.connect(mark_dirty)
        self.row_spin.valueChanged.connect(mark_dirty)

    # ── data plumbing ─────────────────────────────────────────────

    def _load_rows(self) -> list[tuple[str, str]]:
        return [(r.name, f"{r.label}  ·  {r.ownership.value}") for r in roles_repo.list()]

    def _available_skills(self) -> list[str]:
        from ... import skill_scan

        return sorted(s.name for s in skill_scan.scan_skills([Path.cwd()]))

    def _available_mcps(self) -> list[str]:
        from ... import shared_dev_tools

        return sorted(shared_dev_tools.list_master_mcps().keys())

    def _available_plugins(self) -> list[str]:
        from ... import pane_tools_dialog

        return sorted(pane_tools_dialog.discover_marketplace_plugins())

    def _populate_access(self, access, skills_selected: tuple[str, ...]) -> None:
        self._access_forced = access.provider_forced
        idx = self.provider_combo.findText(access.provider)
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.provider_combo.setEnabled(not access.provider_forced)
        self._update_provider_note()

        self.skills_list.clear()
        for name in self._available_skills():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | item.flags().__class__.ItemIsUserCheckable)
            from PyQt6.QtCore import Qt

            item.setCheckState(
                Qt.CheckState.Checked if name in skills_selected else Qt.CheckState.Unchecked
            )
            self.skills_list.addItem(item)

        self.mcps_use_defaults.setChecked(access.mcps is None)
        self.mcps_list.clear()
        for name in self._tri_state_rows(self._available_mcps(), access.mcps):
            self.mcps_list.addItem(name)
        self.mcps_list.setDisabled(access.mcps is None)

        self.plugins_use_defaults.setChecked(access.plugins is None)
        self.plugins_list.clear()
        for name in self._tri_state_rows(self._available_plugins(), access.plugins):
            self.plugins_list.addItem(name)
        self.plugins_list.setDisabled(access.plugins is None)

    def _role_name_for_note(self) -> str:
        if self._current is not None:
            return self._current.name
        return self.name_edit.text().strip().lower()

    def _update_provider_note(self) -> None:
        """Reactive Access-tab notice under the provider combo — recomputed
        on every combo change (not just on page load) so a Lead role that's
        about to lose claude-only capabilities warns the user BEFORE Save,
        per critic R3 blocker #1 (#101 dropped lead from FORCED_ROLES, which
        silently dropped this warning along with the forced lock)."""
        if self._access_forced:
            self.provider_note.setText("Provider fixed by cockpit infrastructure")
            return

        provider = self.provider_combo.currentText()
        if self._role_name_for_note() == "lead" and provider != "claude":
            missing = provider_config.lead_capability_gap_for_provider(provider)
            if missing:
                self.provider_note.setText("⚠ Lead ที่ไม่ใช่ Claude จะเสีย: " + " · ".join(missing))
                return

        self.provider_note.setText("")

    @staticmethod
    def _tri_state_rows(
        available: list[str], selected: tuple[str, ...] | None
    ) -> list[QListWidgetItem]:
        from PyQt6.QtCore import Qt

        items = []
        for name in available:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | item.flags().__class__.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if selected is not None and name in selected
                else Qt.CheckState.Unchecked
            )
            items.append(item)
        return items

    def _load_detail(self, entity_id: str) -> None:
        self._create_mode = False
        self._current = roles_repo.get(entity_id)
        d = self._current
        is_custom = d.ownership is Ownership.CUSTOM

        self.header.set_entity(d.label, d.name, d.ownership)
        self.name_edit.setText(d.name)
        self.name_edit.setEnabled(False)  # id immutable after create
        self.label_edit.setText(d.label)
        self.label_edit.setEnabled(is_custom)
        self.color_edit.setText(d.color)
        self.color_edit.setEnabled(is_custom)
        self.instructions_edit.setPlainText(d.instructions)
        self.instructions_edit.setReadOnly(not is_custom)
        self.column_spin.setValue(d.column)
        self.column_spin.setEnabled(is_custom)
        self.row_spin.setValue(d.row)
        self.row_spin.setEnabled(is_custom)

        self._populate_access(d.access, d.access.skills)

        self.danger_zone.set_plan(
            roles_repo.delete_plan(d.name) if d.capabilities.can_delete else None
        )

        self.footer.set_create_mode(False)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)

    def _start_create(self) -> None:
        self._create_mode = True
        self._current = None
        self.header.set_entity("New Role", "", Ownership.CUSTOM)
        self.name_edit.clear()
        self.name_edit.setEnabled(True)
        self.label_edit.clear()
        self.label_edit.setEnabled(True)
        self.color_edit.setText("#94a3b8")
        self.color_edit.setEnabled(True)
        self.instructions_edit.clear()
        self.instructions_edit.setReadOnly(False)
        self.column_spin.setValue(2)
        self.column_spin.setEnabled(True)
        self.row_spin.setValue(99)
        self.row_spin.setEnabled(True)

        from ..models import RoleAccess

        blank_access = RoleAccess(provider="claude", provider_forced=False, provider_available=True)
        self._populate_access(blank_access, ())
        self.danger_zone.set_plan(None)

        self.footer.set_create_mode(True)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)
        self.name_edit.setFocus()

    def _draft_general(self) -> RoleGeneralDraft:
        return RoleGeneralDraft(
            label=self.label_edit.text().strip(),
            color=self.color_edit.text().strip(),
            column=self.column_spin.value(),
            row=self.row_spin.value(),
            instructions=self.instructions_edit.toPlainText(),
        )

    def _draft_access(self) -> RoleAccessDraft:
        return RoleAccessDraft(
            provider=self.provider_combo.currentText(),
            skills=_checked_names(self.skills_list),
            mcps=None if self.mcps_use_defaults.isChecked() else _checked_names(self.mcps_list),
            plugins=None
            if self.plugins_use_defaults.isChecked()
            else _checked_names(self.plugins_list),
        )

    def _save(self) -> bool:
        if self._create_mode:
            name = self.name_edit.text().strip().lower()
            ok, err = validation.validate_role_name(name)
            if not ok:
                self._show_error(err)
                return False
            result = roles_repo.create(
                CreateRoleCommand(
                    name=name, general=self._draft_general(), access=self._draft_access()
                )
            )
        else:
            assert self._current is not None
            result = roles_repo.update(
                self._current.name,
                UpdateRoleCommand(general=self._draft_general(), access=self._draft_access()),
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

    def _on_delete_confirmed(self, version: str) -> None:
        if self._current is None:
            return
        result = roles_repo.delete(self._current.name, version)
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
