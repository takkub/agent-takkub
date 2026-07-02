"""PaneToolsDialog — per-role MCP/plugin policy editor.

Two-tab QDialog: a role x MCP checkbox matrix and a role x plugin checkbox
matrix. Backed by ``pane_tools_policy`` (persisted policy) and
``shared_dev_tools`` (master MCP registry + role-variant regeneration).

The matrix-building, diffing, and marketplace-plugin-discovery logic is kept
as plain functions (no ``QDialog``/``QApplication`` needed) so it's testable
without a display — only ``PaneToolsDialog`` itself touches Qt.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import json
import pathlib

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Roles that get a row in the matrix. Order matches the cockpit's
# role-declaration convention (lead first, then specialists).
ROLES: tuple[str, ...] = (
    "lead",
    "frontend",
    "backend",
    "mobile",
    "devops",
    "qa",
    "reviewer",
    "critic",
    "designer",
)

# Always-DENIED regardless of policy — lead_context._PANE_PLUGIN_DENYLIST
# hard-blocks these hook-heavy plugins from every pane (slow spawn / crash
# surface). The dialog disables their checkboxes so the UI doesn't pretend
# a click could enable them.
_FORCED_PLUGINS = frozenset({"security-guidance", "remember"})

_PLUGINS_INSTALLED_FILE = pathlib.Path.home() / ".claude" / "plugins" / "installed_plugins.json"


# ──────────────────────────────────────────────────────────────────
# pure logic (testable without QApplication)
# ──────────────────────────────────────────────────────────────────


def build_matrix(
    roles: tuple[str, ...],
    items: list[str],
    role_items: dict[str, list[str]],
) -> dict[str, dict[str, bool]]:
    """Build ``matrix[role][item] = bool`` from each role's current item list.

    ``role_items`` maps role -> list of item names currently enabled for
    that role (already resolved through defaults by the caller, e.g. via
    ``pane_tools_policy.effective_mcps``).
    """
    matrix: dict[str, dict[str, bool]] = {}
    for role in roles:
        enabled = set(role_items.get(role, ()))
        matrix[role] = {item: item in enabled for item in items}
    return matrix


def matrix_to_role_items(matrix: dict[str, dict[str, bool]]) -> dict[str, list[str]]:
    """Inverse of ``build_matrix``: checked items per role, name-sorted."""
    return {
        role: sorted(item for item, checked in items.items() if checked)
        for role, items in matrix.items()
    }


def diff_role_items(
    original: dict[str, list[str]],
    updated: dict[str, list[str]],
) -> dict[str, tuple[list[str], list[str]]]:
    """Per-role ``(added, removed)`` item names between two role->items maps.

    Roles with no change are omitted so the caller only touches what
    actually changed.
    """
    changes: dict[str, tuple[list[str], list[str]]] = {}
    for role in set(original) | set(updated):
        before = set(original.get(role, ()))
        after = set(updated.get(role, ()))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            changes[role] = (added, removed)
    return changes


def discover_marketplace_plugins(
    installed_file: pathlib.Path = _PLUGINS_INSTALLED_FILE,
) -> list[str]:
    """Plugin names (``name@marketplace``) known to this machine's Claude
    plugin install registry. Missing/unreadable file -> empty list (dialog
    just shows no plugin columns, doesn't crash)."""
    try:
        data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return []
    return sorted(plugins.keys())


def parse_install_form(name: str, command: str, args_line: str) -> tuple[str, dict] | None:
    """Turn the "add MCP" form fields into ``(name, cfg)`` for
    ``shared_dev_tools.add_mcp_server``. Returns ``None`` if the form is
    incomplete (caller shows a validation message instead of calling out)."""
    name = name.strip()
    command = command.strip()
    if not name or not command:
        return None
    args = args_line.split()
    return name, {"command": command, "args": args}


# ──────────────────────────────────────────────────────────────────
# Qt dialog
# ──────────────────────────────────────────────────────────────────


class PaneToolsDialog(QDialog):
    """Settings dialog for per-role MCP + plugin visibility.

    Loads the current policy on open, lets the user tick/untick per-role
    checkboxes, and only writes back (``pane_tools_policy.save_policy`` +
    ``shared_dev_tools.regen_role_variants``) when Save is clicked.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pane Tools — MCP & Plugin policy")
        self.resize(760, 480)

        self._mcp_boxes: dict[str, dict[str, QCheckBox]] = {}
        self._plugin_boxes: dict[str, dict[str, QCheckBox]] = {}
        self._orig_mcp_items: dict[str, list[str]] = {}
        self._orig_plugin_items: dict[str, list[str]] = {}

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)

        tabs.addTab(self._build_mcp_tab(), "MCP")
        tabs.addTab(self._build_plugin_tab(), "Plugins")

        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: #71717a; font-size: 11px;")
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(self)
        self._btn_reset = buttons.addButton(
            "Reset to default", QDialogButtonBox.ButtonRole.ResetRole
        )
        self._btn_save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_close = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_reset.clicked.connect(self._on_reset_clicked)
        self._btn_save.clicked.connect(self._on_save_clicked)
        self._btn_close.clicked.connect(self.reject)
        layout.addWidget(buttons)

    # ── MCP tab ──────────────────────────────────────────────────

    def _build_mcp_tab(self) -> QWidget:
        tab = QWidget(self)
        outer = QVBoxLayout(tab)

        add_row = QHBoxLayout()
        self._btn_add_mcp = QPushButton("+ เพิ่ม MCP…", tab)
        self._btn_add_mcp.clicked.connect(self._on_add_mcp_clicked)
        self._btn_remove_mcp = QPushButton("ลบ MCP ที่เลือก", tab)
        self._btn_remove_mcp.clicked.connect(self._on_remove_mcp_clicked)
        add_row.addWidget(self._btn_add_mcp)
        add_row.addWidget(self._btn_remove_mcp)
        add_row.addStretch(1)
        outer.addLayout(add_row)

        self._mcp_table = QTableWidget(tab)
        outer.addWidget(self._mcp_table)
        self._reload_mcp_table()
        return tab

    def _master_mcps(self) -> list[str]:
        try:
            from . import shared_dev_tools

            return list(shared_dev_tools.list_master_mcps())
        except Exception:
            return []

    def _policy_role_items(self, kind: str) -> dict[str, list[str]]:
        """Current per-role item names for *kind* ('mcps'/'plugins'),
        resolved through ``pane_tools_policy`` defaults."""
        try:
            from . import pane_tools_policy, shared_dev_tools
            from .lead_context import _ROLE_PLUGIN_POLICY, _TEAMMATE_PLUGINS

            defaults = getattr(shared_dev_tools, "_ROLE_MCP_POLICY", {})
            result: dict[str, list[str]] = {}
            for role in ROLES:
                if kind == "mcps":
                    default = frozenset(defaults.get(role, ()))
                    result[role] = list(pane_tools_policy.effective_mcps(role, default) or ())
                else:
                    # Real built-in default, NOT [] — otherwise the matrix
                    # renders every plugin unchecked and a naive Save writes
                    # deny-all overrides for every role.
                    default = _ROLE_PLUGIN_POLICY.get(role, _TEAMMATE_PLUGINS)
                    result[role] = list(pane_tools_policy.effective_plugins(role, default) or ())
            return result
        except Exception:
            return {role: [] for role in ROLES}

    def _reload_mcp_table(self) -> None:
        items = self._master_mcps()
        self._orig_mcp_items = self._policy_role_items("mcps")
        matrix = build_matrix(ROLES, items, self._orig_mcp_items)
        self._mcp_boxes = self._fill_matrix_table(self._mcp_table, items, matrix)

    def _build_plugin_tab(self) -> QWidget:
        tab = QWidget(self)
        outer = QVBoxLayout(tab)

        note = QLabel(
            "security-guidance และ remember ถูก denylist ปิดเสมอทุก pane "
            "(hook หนัก ทำ spawn ช้า) — policy นี้เปิดให้ไม่ได้.",
            tab,
        )
        note.setStyleSheet("color: #71717a; font-size: 11px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        self._plugin_table = QTableWidget(tab)
        outer.addWidget(self._plugin_table)
        self._reload_plugin_table()
        return tab

    def _reload_plugin_table(self) -> None:
        items = discover_marketplace_plugins()
        self._orig_plugin_items = self._policy_role_items("plugins")
        matrix = build_matrix(ROLES, items, self._orig_plugin_items)
        self._plugin_boxes = self._fill_matrix_table(
            self._plugin_table, items, matrix, disabled_items=_FORCED_PLUGINS
        )

    # ── shared matrix table builder ─────────────────────────────

    def _fill_matrix_table(
        self,
        table: QTableWidget,
        items: list[str],
        matrix: dict[str, dict[str, bool]],
        disabled_items: frozenset[str] = frozenset(),
    ) -> dict[str, dict[str, QCheckBox]]:
        table.clear()
        table.setRowCount(len(ROLES))
        table.setColumnCount(len(items))
        table.setHorizontalHeaderLabels(items)
        table.setVerticalHeaderLabels(list(ROLES))

        boxes: dict[str, dict[str, QCheckBox]] = {}
        for row, role in enumerate(ROLES):
            boxes[role] = {}
            for col, item in enumerate(items):
                box = QCheckBox(table)
                box.setChecked(matrix.get(role, {}).get(item, False))
                if item in disabled_items:
                    box.setChecked(True)
                    box.setEnabled(False)
                container = QWidget(table)
                cell_layout = QHBoxLayout(container)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                cell_layout.addWidget(box, alignment=Qt.AlignmentFlag.AlignCenter)
                table.setCellWidget(row, col, container)
                boxes[role][item] = box
        table.resizeColumnsToContents()
        return boxes

    # ── add/remove MCP ──────────────────────────────────────────

    def _on_add_mcp_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("เพิ่ม MCP")
        form = QFormLayout(dlg)
        name_edit = QLineEdit(dlg)
        command_edit = QLineEdit(dlg)
        args_edit = QLineEdit(dlg)
        args_edit.setPlaceholderText("-y some-mcp-package (เว้นวรรคคั่น)")
        form.addRow("ชื่อ", name_edit)
        form.addRow("Command", command_edit)
        form.addRow("Args", args_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        parsed = parse_install_form(name_edit.text(), command_edit.text(), args_edit.text())
        if parsed is None:
            QMessageBox.warning(self, "เพิ่ม MCP", "กรอกชื่อและ command ให้ครบ")
            return
        name, cfg = parsed
        try:
            from . import shared_dev_tools

            shared_dev_tools.add_mcp_server(name, cfg)
        except Exception as e:
            QMessageBox.warning(self, "เพิ่ม MCP ไม่สำเร็จ", str(e))
            return
        self._reload_mcp_table()
        self._status_label.setText(f"เพิ่ม MCP '{name}' แล้ว — กด Save เพื่อ apply ต่อ role")

    def _on_remove_mcp_clicked(self) -> None:
        cols = {idx.column() for idx in self._mcp_table.selectedIndexes()}
        if not cols:
            QMessageBox.information(self, "ลบ MCP", "เลือกคอลัมน์ MCP ที่ต้องการลบก่อน")
            return
        names = [self._mcp_table.horizontalHeaderItem(c).text() for c in cols]
        confirm = QMessageBox.question(
            self,
            "ลบ MCP",
            f"ลบ MCP: {', '.join(names)} ออกจาก master registry?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            from . import shared_dev_tools

            for name in names:
                shared_dev_tools.remove_mcp_server(name)
        except Exception as e:
            QMessageBox.warning(self, "ลบ MCP ไม่สำเร็จ", str(e))
            return
        self._reload_mcp_table()
        self._status_label.setText(f"ลบ MCP: {', '.join(names)} แล้ว — กด Save เพื่อ apply ต่อ role")

    # ── save / reset ─────────────────────────────────────────────

    def _current_matrix_items(self, boxes: dict[str, dict[str, QCheckBox]]) -> dict[str, list[str]]:
        return matrix_to_role_items(
            {
                role: {item: box.isChecked() for item, box in items.items()}
                for role, items in boxes.items()
            }
        )

    def _on_save_clicked(self) -> None:
        updated_mcps = self._current_matrix_items(self._mcp_boxes)
        updated_plugins = self._current_matrix_items(self._plugin_boxes)
        mcp_changes = diff_role_items(self._orig_mcp_items, updated_mcps)
        plugin_changes = diff_role_items(self._orig_plugin_items, updated_plugins)

        try:
            from . import pane_tools_policy, shared_dev_tools

            for role in mcp_changes:
                pane_tools_policy.set_role_items(role, "mcps", updated_mcps[role])
            for role in plugin_changes:
                pane_tools_policy.set_role_items(role, "plugins", updated_plugins[role])
            shared_dev_tools.regen_role_variants()
        except Exception as e:
            QMessageBox.warning(self, "Save ไม่สำเร็จ", str(e))
            return

        self._orig_mcp_items = updated_mcps
        self._orig_plugin_items = updated_plugins
        changed_roles = sorted(set(mcp_changes) | set(plugin_changes))
        if changed_roles:
            self._status_label.setText(
                "บันทึกแล้ว — มีผลกับ pane ที่ spawn ใหม่ (role ที่เปลี่ยน: " + ", ".join(changed_roles) + ")"
            )
        else:
            self._status_label.setText("ไม่มีอะไรเปลี่ยน")

    def _on_reset_clicked(self) -> None:
        try:
            from . import pane_tools_policy

            for role in ROLES:
                pane_tools_policy.reset_role(role)
        except Exception as e:
            QMessageBox.warning(self, "Reset ไม่สำเร็จ", str(e))
            return
        self._reload_mcp_table()
        self._reload_plugin_table()
        self._status_label.setText("Reset เป็น default ต่อ role แล้ว")
