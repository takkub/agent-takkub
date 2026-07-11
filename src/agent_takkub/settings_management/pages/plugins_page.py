"""Plugins vertical slice (SPEC.md §Plugins).

Everything on this page is either read-only external metadata (identity,
version, install path — owned by the marketplace, not the cockpit) or a
read-only summary of role assignment (owned by ``pane_tools_policy``, edited
from the Role's Access tab — SPEC.md "Relationships"). The only WRITE
surfaces this page has are Install (create) and Uninstall (delete); there is
no "Save" of an edited definition, unlike MCP Servers' USER-owned servers.

Wording matches capability exactly (SPEC.md "คำใน UI ต้องตรง capability
จริง"): create = **Install Plugin**, delete = **Uninstall**, never
New/Delete. ``DangerZone``/``DetailFooter`` (``widgets/``) hardcode "Delete"/
"Create" — out of scope to edit here (a parallel pane owns ``widgets/``), so
this page relabels those two buttons post-construction instead.

Denylisted plugin identities (``security-guidance``, ``remember`` —
``lead_context._PANE_PLUGIN_DENYLIST``) show a **BLOCKED BY COCKPIT** banner
with the reason and never list an allowed role (assignment disabled), per
SPEC.md §Plugins.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from ..commands import CreatePluginCommand
from ..models import Ownership, PluginDetail
from ..repositories import plugins as plugins_repo
from ..widgets.danger_zone import DangerZone
from ..widgets.detail_footer import DetailFooter
from ..widgets.detail_header import DetailHeader
from ..widgets.management_page import ManagementPage


class PluginsPage(ManagementPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Plugins",
            filters=("All", "Blocked"),
            new_button_label="+ Install Plugin",
            parent=parent,
        )

        self._detail = QWidget(self)
        detail_layout = QVBoxLayout(self._detail)
        self.header = DetailHeader(self._detail)
        detail_layout.addWidget(self.header)

        self.blocked_banner = QLabel(self._detail)
        self.blocked_banner.setWordWrap(True)
        self.blocked_banner.setStyleSheet(
            f"background: {theme.ERROR_CHIP_BG}; border: 1px solid {theme.ERROR_CHIP_BORDER};"
            f"color: {theme.ERROR_CHIP_TEXT}; border-radius: {theme.RADIUS_SM}px; padding: 8px 12px;"
        )
        self.blocked_banner.setVisible(False)
        detail_layout.addWidget(self.blocked_banner)

        self.tabs = QTabWidget(self._detail)
        detail_layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_roles_tab()

        self.danger_zone = DangerZone(self._detail)
        # widgets/danger_zone.py hardcodes "Delete" — relabel, don't fork the
        # shared widget (see module docstring).
        self.danger_zone._delete_btn.setText("Uninstall")
        self.danger_zone.delete_requested.connect(self._on_delete_confirmed)
        detail_layout.addWidget(self.danger_zone)

        self.footer = DetailFooter(self._detail)
        self.footer.save_clicked.connect(self._on_save_clicked)
        self.footer.discard_clicked.connect(self._on_discard_clicked)
        detail_layout.addWidget(self.footer)

        self.detail_stack.addWidget(self._detail)

        self._current: PluginDetail | None = None
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
        self.key_edit = QLineEdit()
        self.marketplace_combo = QComboBox()
        self.marketplace_combo.setEditable(True)
        self.version_label = QLabel("")
        self.scope_label = QLabel("")
        self.enabled_label = QLabel("")
        self.install_path_label = QLabel("")
        self.install_path_label.setWordWrap(True)
        self.governance_label = QLabel("")
        self.governance_label.setWordWrap(True)
        self.governance_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        form.addRow("Key", self.key_edit)
        form.addRow("Marketplace", self.marketplace_combo)
        form.addRow("Version", self.version_label)
        form.addRow("Scope", self.scope_label)
        form.addRow("Enabled", self.enabled_label)
        form.addRow("Install path", self.install_path_label)
        form.addRow("Governance", self.governance_label)
        self.tabs.addTab(tab, "General")

    def _build_roles_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.roles_note = QLabel(
            "Roles that currently see this plugin's marketplace "
            "(read-only — edit from the role's Access tab):"
        )
        self.roles_note.setWordWrap(True)
        layout.addWidget(self.roles_note)
        self.roles_list = QListWidget()
        layout.addWidget(self.roles_list)
        self.manage_roles_btn = theme.secondary_button("Manage roles", tab)
        self.manage_roles_btn.clicked.connect(lambda: self.manage_roles_requested())
        layout.addWidget(self.manage_roles_btn)
        self.tabs.addTab(tab, "Allowed roles")

    def manage_roles_requested(self) -> None:
        """Reassigned by the window shell to jump to the Roles page — a
        hook-attribute like `load_rows`/`on_select`, not a Qt signal, so the
        page stays importable/testable without a shell present."""

    def _wire_dirty_tracking(self) -> None:
        def mark_dirty(*_a: object) -> None:
            if self._create_mode:
                self._dirty = True
                self.footer.set_dirty(True)

        self.key_edit.textChanged.connect(mark_dirty)
        self.marketplace_combo.currentTextChanged.connect(mark_dirty)

    # ── data plumbing ─────────────────────────────────────────────

    def _load_rows(self) -> list[tuple[str, str]]:
        rows = []
        for p in plugins_repo.list():
            label = f"{p.id}  ·  BLOCKED" if p.blocked else p.id
            rows.append((p.id, label))
        return rows

    def _load_detail(self, entity_id: str) -> None:
        self._create_mode = False
        self._current = plugins_repo.get(entity_id)
        d = self._current

        self.header.set_entity(d.key, d.id, d.ownership)

        self.key_edit.setText(d.key)
        self.key_edit.setEnabled(False)
        self.marketplace_combo.clear()
        self.marketplace_combo.addItem(d.marketplace)
        self.marketplace_combo.setEnabled(False)
        self.version_label.setText(d.version or "(unknown)")
        self.scope_label.setText(d.scope or "(unknown)")
        self.enabled_label.setText("✓ enabled" if d.enabled else "✗ disabled")
        self.install_path_label.setText(d.install_path or "(unknown)")
        self.governance_label.setText(
            "Marketplace นี้อยู่ในชุดที่ cockpit inject เข้า pane ได้"
            if d.governable
            else "Marketplace นี้ไม่ได้อยู่ในชุดที่ cockpit inject เข้า pane — "
            "ติดตั้งไว้ใช้เองเท่านั้น ไม่มี role ไหนได้รับผ่าน panel นี้"
        )

        if d.blocked:
            self.blocked_banner.setText(f"BLOCKED BY COCKPIT — {d.blocked_reason}")
        self.blocked_banner.setVisible(d.blocked)

        self.roles_list.clear()
        if d.blocked:
            self.roles_list.addItem("(assignment disabled — blocked by cockpit)")
        else:
            self.roles_list.addItems(list(d.allowed_roles) or ["(ไม่มี role ไหนเห็น plugin นี้)"])

        self.danger_zone.set_plan(
            plugins_repo.delete_plan(d.id) if d.capabilities.can_delete else None
        )

        self.footer.set_create_mode(False)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)

    def _start_create(self) -> None:
        self._create_mode = True
        self._current = None
        self.header.set_entity("New Plugin", "", Ownership.EXTERNAL)
        self.blocked_banner.setVisible(False)
        self.key_edit.clear()
        self.key_edit.setEnabled(True)
        self.marketplace_combo.clear()
        self.marketplace_combo.addItems(sorted(plugins_repo.governable_marketplaces()))
        self.marketplace_combo.setCurrentText("")
        self.marketplace_combo.setEnabled(True)
        self.version_label.setText("")
        self.scope_label.setText("")
        self.enabled_label.setText("")
        self.install_path_label.setText("")
        self.governance_label.setText(
            "เลือก marketplace ที่ cockpit inject เข้า pane ได้ — ปล่อยว่างได้ถ้า plugin "
            "อยู่ marketplace เดียวที่ลงทะเบียนไว้"
        )
        self.roles_list.clear()
        self.danger_zone.set_plan(None)

        self.footer.set_create_mode(True)
        # widgets/detail_footer.py hardcodes "Create" — relabel to match
        # capability wording (see module docstring).
        self.footer._save_btn.setText("Install Plugin")
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)
        self.key_edit.setFocus()

    def _save(self) -> bool:
        if not self._create_mode:
            return True  # nothing editable outside create mode
        result = plugins_repo.create(
            CreatePluginCommand(
                key=self.key_edit.text().strip(),
                marketplace=self.marketplace_combo.currentText().strip(),
            )
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
            self._load_detail(self._current.id)
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
        result = plugins_repo.delete(self._current.id, version)
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
