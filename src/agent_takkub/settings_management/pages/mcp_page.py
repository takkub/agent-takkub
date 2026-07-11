"""MCP Servers vertical slice (SPEC.md §MCP Servers).

General (type/command/args/env) · Allowed roles (read-only summary — editing
happens from the Role's Access tab, per SPEC.md "Relationships") ·
Diagnostics (command found/not found; never runs a long-lived server from
Settings). MANAGED (browser) servers keep General read-only; USER servers
get full CRUD. Credential-bearing config is always shown masked.
"""

from __future__ import annotations

import shutil

from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from ..commands import CreateMcpCommand, McpConfigDraft, UpdateMcpCommand
from ..models import McpDetail, Ownership
from ..repositories import mcps as mcps_repo
from ..widgets.danger_zone import DangerZone
from ..widgets.detail_footer import DetailFooter
from ..widgets.detail_header import DetailHeader
from ..widgets.management_page import ManagementPage


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _format_env(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in env.items())


class McpPage(ManagementPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "MCP Servers",
            filters=("All", "Managed", "User"),
            new_button_label="+ New MCP Server",
            parent=parent,
        )

        self._detail = QWidget(self)
        detail_layout = QVBoxLayout(self._detail)
        self.header = DetailHeader(self._detail)
        detail_layout.addWidget(self.header)

        self.secret_warning = QLabel(
            "⚠️ config นี้มี credential — ค่าที่โชว์ถูก mask ไว้ แก้ค่าจริงผ่าน env ของเครื่องนี้"
        )
        self.secret_warning.setWordWrap(True)
        self.secret_warning.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.secret_warning.setVisible(False)
        detail_layout.addWidget(self.secret_warning)

        self.tabs = QTabWidget(self._detail)
        detail_layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_roles_tab()
        self._build_diagnostics_tab()

        self.danger_zone = DangerZone(self._detail)
        self.danger_zone.delete_requested.connect(self._on_delete_confirmed)
        detail_layout.addWidget(self.danger_zone)

        self.footer = DetailFooter(self._detail)
        self.footer.save_clicked.connect(self._on_save_clicked)
        self.footer.discard_clicked.connect(self._on_discard_clicked)
        detail_layout.addWidget(self.footer)

        self.detail_stack.addWidget(self._detail)

        self._current: McpDetail | None = None
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
        self.type_combo = QComboBox()
        self.type_combo.addItems(["stdio", "http", "sse"])
        self.command_edit = QLineEdit()
        self.args_edit = QPlainTextEdit()
        self.args_edit.setPlaceholderText("one argument per line")
        self.args_edit.setMaximumHeight(90)
        self.env_edit = QPlainTextEdit()
        self.env_edit.setPlaceholderText("KEY=value, one per line")
        self.env_edit.setMaximumHeight(90)
        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.type_combo)
        form.addRow("Command", self.command_edit)
        form.addRow("Args", self.args_edit)
        form.addRow("Env", self.env_edit)
        self.tabs.addTab(tab, "General")

    def _build_roles_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(
            QLabel(
                "Roles that currently see this MCP (read-only — edit from the role's Access tab):"
            )
        )
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

    def _build_diagnostics_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.diagnostics_label = QLabel("")
        self.diagnostics_label.setWordWrap(True)
        layout.addWidget(self.diagnostics_label)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Diagnostics")

    def _wire_dirty_tracking(self) -> None:
        def mark_dirty(*_a: object) -> None:
            if self._current is not None or self._create_mode:
                self._dirty = True
                self.footer.set_dirty(True)

        self.name_edit.textChanged.connect(mark_dirty)
        self.type_combo.currentIndexChanged.connect(mark_dirty)
        self.command_edit.textChanged.connect(mark_dirty)
        self.args_edit.textChanged.connect(mark_dirty)
        self.env_edit.textChanged.connect(mark_dirty)

    # ── data plumbing ─────────────────────────────────────────────

    def _load_rows(self) -> list[tuple[str, str]]:
        return [(m.name, f"{m.name}  ·  {m.ownership.value}") for m in mcps_repo.list()]

    def _load_detail(self, entity_id: str) -> None:
        self._create_mode = False
        self._current = mcps_repo.get(entity_id)
        d = self._current
        writable = d.capabilities.can_update

        self.header.set_entity(d.name, d.name, d.ownership)
        self.name_edit.setText(d.name)
        self.name_edit.setEnabled(False)  # name immutable after create
        idx = self.type_combo.findText(str(d.config.get("type", "stdio")))
        self.type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.type_combo.setEnabled(writable)
        self.command_edit.setText(str(d.config.get("command", "")))
        self.command_edit.setEnabled(writable)
        self.args_edit.setPlainText("\n".join(str(a) for a in d.config.get("args") or []))
        self.args_edit.setReadOnly(not writable)
        self.env_edit.setPlainText(_format_env(d.config.get("env") or {}))
        self.env_edit.setReadOnly(not writable)

        self.secret_warning.setVisible(d.has_secrets)

        self.roles_list.clear()
        self.roles_list.addItems(list(d.allowed_roles) or ["(ไม่มี role ไหนเห็น MCP นี้)"])

        command = str(d.config.get("command", ""))
        found = shutil.which(command) if command else None
        self.diagnostics_label.setText(
            f"Command: {command or '(none)'}\n"
            f"Found on PATH: {'✓ ' + found if found else '✗ ไม่พบ'}\n"
            f"Config source: {'Cockpit browser MCP (managed)' if d.ownership is Ownership.MANAGED else 'User-added'}"
        )

        self.danger_zone.set_plan(mcps_repo.delete_plan(d.name) if writable else None)

        self.footer.set_create_mode(False)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)

    def _start_create(self) -> None:
        self._create_mode = True
        self._current = None
        self.header.set_entity("New MCP Server", "", Ownership.USER)
        self.name_edit.clear()
        self.name_edit.setEnabled(True)
        self.type_combo.setCurrentIndex(0)
        self.type_combo.setEnabled(True)
        self.command_edit.clear()
        self.command_edit.setEnabled(True)
        self.args_edit.clear()
        self.args_edit.setReadOnly(False)
        self.env_edit.clear()
        self.env_edit.setReadOnly(False)
        self.secret_warning.setVisible(False)
        self.roles_list.clear()
        self.diagnostics_label.setText("")
        self.danger_zone.set_plan(None)

        self.footer.set_create_mode(True)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)
        self.name_edit.setFocus()

    def _draft(self) -> McpConfigDraft:
        args = [line.strip() for line in self.args_edit.toPlainText().splitlines() if line.strip()]
        return McpConfigDraft(
            command=self.command_edit.text().strip(),
            args=args,
            env=_parse_env(self.env_edit.toPlainText()),
            type=self.type_combo.currentText(),
        )

    def _save(self) -> bool:
        if self._create_mode:
            name = self.name_edit.text().strip().lower()
            result = mcps_repo.create(CreateMcpCommand(name=name, config=self._draft()))
        else:
            assert self._current is not None
            result = mcps_repo.update(self._current.name, UpdateMcpCommand(config=self._draft()))

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
        result = mcps_repo.delete(self._current.name, version)
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
