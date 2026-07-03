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
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
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

_PLUGINS_INSTALLED_FILE = pathlib.Path.home() / ".claude" / "plugins" / "installed_plugins.json"

# Zinc dark theme, matched to the cockpit shell (MainWindow QSS + status-bar
# chips): #09090b bg, #18181b/#27272a surfaces, zinc text ramp, #2563eb accent
# for the one consequential action (Save). A QDialog stylesheet cascades to all
# descendant widgets — including the child "add MCP" dialog and QMessageBoxes —
# so styling here themes the whole feature in one place.
_DIALOG_QSS = """
QDialog { background-color: #09090b; color: #e4e4e7; font-size: 12px; }
QLabel { color: #d4d4d8; background: transparent; }
QLabel#toolsTitle { color: #fafafa; font-size: 16px; font-weight: 700; }
QLabel#toolsSubtitle { color: #a1a1aa; font-size: 11px; }
QLabel#emptyState { color: #a1a1aa; font-size: 12px; padding: 40px; }

QTabWidget::pane {
    border: 1px solid #27272a; border-radius: 8px; top: -1px; background: #18181b;
}
QTabBar::tab {
    background: transparent; color: #a1a1aa; padding: 6px 18px; margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 8px; border-top-right-radius: 8px; font-weight: 500;
}
QTabBar::tab:selected {
    background: #18181b; color: #fafafa;
    border: 1px solid #27272a; border-bottom-color: #18181b;
}
QTabBar::tab:hover:!selected { color: #e4e4e7; }

QTableWidget {
    background: #0c0c0e; alternate-background-color: #131316;
    gridline-color: #1f1f23; border: none; border-radius: 8px;
    color: #d4d4d8; outline: none;
}
QTableWidget::item { padding: 0; border-bottom: 1px solid #1f1f23; }
QTableWidget::item:selected { background: rgba(37,99,235,0.16); }
QTableWidget QWidget { background: transparent; }

QHeaderView { background: transparent; }
QHeaderView::section {
    background: #18181b; color: #a1a1aa; padding: 7px 10px; border: none;
    border-right: 1px solid #1f1f23; border-bottom: 1px solid #27272a;
    font-weight: 600;
}
QHeaderView::section:hover { color: #e4e4e7; background: #1f1f23; }
QHeaderView::section:vertical {
    color: #e4e4e7; padding-left: 12px; border-right: 1px solid #27272a;
}
QTableCornerButton::section {
    background: #18181b; border: none;
    border-bottom: 1px solid #27272a; border-right: 1px solid #27272a;
}

QCheckBox { spacing: 0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #3f3f46;
    border-radius: 4px; background: #18181b;
}
QCheckBox::indicator:hover { border-color: #52525b; }
QCheckBox::indicator:checked { background: #2563eb; border-color: #2563eb;%CHECK% }
QCheckBox::indicator:unchecked:disabled { background: #131316; border-color: #27272a; }

QLineEdit {
    background: #18181b; border: 1px solid #27272a; border-radius: 6px;
    padding: 6px 8px; color: #e4e4e7; selection-background-color: #2563eb;
}
QLineEdit:focus { border-color: #2563eb; }

QPushButton {
    background: #18181b; color: #d4d4d8; border: 1px solid #27272a;
    border-radius: 6px; padding: 8px 16px; font-weight: 500;
}
QPushButton:hover { background: #27272a; border-color: #3f3f46; }
QPushButton:pressed { background: #131316; }
QPushButton#primaryBtn {
    background: #2563eb; color: #ffffff; border: 1px solid #2563eb; font-weight: 600;
}
QPushButton#primaryBtn:hover { background: #3b82f6; border-color: #3b82f6; }
QPushButton#primaryBtn:pressed { background: #1d4ed8; }
"""

# Prettier role labels for the vertical header — logic keys stay lowercase
# (see ``ROLES``); this is display-only.
_ROLE_DISPLAY: dict[str, str] = {"qa": "QA", "devops": "DevOps"}


def role_label(role: str) -> str:
    """Human-facing role name for the matrix's vertical header."""
    return _ROLE_DISPLAY.get(role, role.capitalize())


def _checkmark_icon_path() -> str:
    """Path to a small white ✓ PNG for the checked-checkbox indicator.

    A solid blue square alone reads as "selected cell", and relies on colour
    as the only state cue (fails shape-based/low-vision users). We draw the
    tick once with QPainter — no bundled asset, no QtSvg dependency — cache it
    under ``~/.takkub/cache``, and hand the path to the stylesheet. Any failure
    returns ``""`` so the QSS simply falls back to the plain blue fill.
    """
    try:
        cache = pathlib.Path.home() / ".takkub" / "cache"
        path = cache / "tools-check.png"
        if not path.exists():
            from PyQt6.QtCore import QPointF
            from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

            cache.mkdir(parents=True, exist_ok=True)
            img = QImage(16, 16, QImage.Format.Format_ARGB32)
            img.fill(0)  # transparent
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor("#ffffff"))
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(
                QPolygonF([QPointF(3.5, 8.5), QPointF(6.7, 11.5), QPointF(12.5, 4.7)])
            )
            painter.end()
            img.save(str(path), "PNG")
        return path.as_posix()
    except Exception:
        return ""


def compose_qss() -> str:
    """The dialog stylesheet with the checkmark asset URL substituted in."""
    check = _checkmark_icon_path()
    mark = f' image: url("{check}");' if check else ""
    return _DIALOG_QSS.replace("%CHECK%", mark)


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


def discover_marketplaces(
    installed_file: pathlib.Path = _PLUGINS_INSTALLED_FILE,
) -> list[str]:
    """Marketplace names the pane plugin-policy can actually govern.

    The plugin policy is **marketplace-granular**: ``_default_plugin_dirs``
    filters by marketplace and loads every plugin dir under an allowed one, so a
    role's effective plugin set is a set of *marketplace* names
    (``superpowers-dev``, ``pordee``, ``claude-plugins-official``) — never
    ``name@marketplace``. The Plugins matrix must therefore offer **marketplace
    columns** so a checkbox's identity matches what the policy stores and reads.

    Using ``discover_marketplace_plugins`` (``name@marketplace``) as columns made
    every cell compare e.g. ``code-review@claude-plugins-official`` against a
    ``claude-plugins-official`` policy entry — never equal, so the whole grid
    rendered unchecked even when the plugins were enabled, and a Save then wrote
    an empty deny-all override for every role. That was the 2026-07-02 wipe.

    Returns the installed marketplaces intersected with ``_SAFE_PLUGINS`` (the
    only ones pane injection can load), sorted. Missing/unreadable file → [].
    """
    from .config import _SAFE_PLUGINS

    try:
        data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return []
    governable = set(_SAFE_PLUGINS)
    # Install-registry keys are ``name@marketplace``; the segment after ``@`` is
    # the marketplace. Keep only marketplaces the pane loader can inject.
    found = {
        key.partition("@")[2]
        for key in plugins
        if isinstance(key, str) and key.partition("@")[2] in governable
    }
    return sorted(found)


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


class _CheckCell(QWidget):
    """Matrix cell whose *entire* area toggles the contained checkbox.

    The checkbox indicator is only 16px; when it's the sole hit target,
    clicks that land a few pixels off do nothing (or select the column).
    Wrapping it here and toggling on any press makes the whole cell a
    comfortable click target. Presses that land directly on the checkbox are
    consumed by it, so there's no double-toggle.
    """

    def __init__(self, box: QCheckBox, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._box = box
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(box, alignment=Qt.AlignmentFlag.AlignCenter)

    def mousePressEvent(self, ev) -> None:
        if self._box.isEnabled():
            self._box.toggle()
        super().mousePressEvent(ev)


class PaneToolsDialog(QDialog):
    """Settings dialog for per-role MCP + plugin visibility.

    Loads the current policy on open, lets the user tick/untick per-role
    checkboxes, and only writes back (``pane_tools_policy.save_policy`` +
    ``shared_dev_tools.regen_role_variants``) when Save is clicked.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pane Tools — MCP & Plugin policy")
        self.resize(820, 540)
        self.setStyleSheet(compose_qss())

        self._mcp_boxes: dict[str, dict[str, QCheckBox]] = {}
        self._plugin_boxes: dict[str, dict[str, QCheckBox]] = {}
        self._orig_mcp_items: dict[str, list[str]] = {}
        self._orig_plugin_items: dict[str, list[str]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        # Header block — makes the dialog read as an intentional settings panel
        # rather than a bare table dropped on a window. Title + subtitle are
        # grouped in a tight sub-layout (proximity) so they read as one unit,
        # distinct from the tabs below.
        header = QVBoxLayout()
        header.setSpacing(3)
        title = QLabel("🔧 Pane Tools", self)
        title.setObjectName("toolsTitle")
        subtitle = QLabel(
            "เปิด/ปิด MCP และ plugin ต่อ role — ติ๊กช่องแล้วกด Save (มีผลกับ pane ที่ spawn ใหม่)",
            self,
        )
        subtitle.setObjectName("toolsSubtitle")
        subtitle.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        tabs = QTabWidget(self)
        layout.addWidget(tabs, 1)

        tabs.addTab(self._build_mcp_tab(), "MCP")
        tabs.addTab(self._build_plugin_tab(), "Plugins")

        self._status_label = QLabel("", self)
        self._status_label.setObjectName("toolsSubtitle")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(self)
        self._btn_reset = buttons.addButton(
            "Reset to default", QDialogButtonBox.ButtonRole.ResetRole
        )
        self._btn_save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_save.setObjectName("primaryBtn")
        self._btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_reset.clicked.connect(self._on_reset_clicked)
        self._btn_save.clicked.connect(self._on_save_clicked)
        self._btn_close.clicked.connect(self.reject)
        layout.addWidget(buttons)

    # ── MCP tab ──────────────────────────────────────────────────

    def _build_mcp_tab(self) -> QWidget:
        tab = QWidget(self)
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self._btn_add_mcp = QPushButton("+ เพิ่ม MCP…", tab)
        self._btn_add_mcp.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_mcp.clicked.connect(self._on_add_mcp_clicked)
        self._btn_remove_mcp = QPushButton("ลบ MCP ที่เลือก", tab)
        self._btn_remove_mcp.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_remove_mcp.setToolTip("คลิกหัวคอลัมน์ MCP เพื่อเลือก แล้วกดปุ่มนี้ (หรือคลิกขวาที่หัวคอลัมน์)")
        # Disabled until a column is selected — clicking it empty just to be
        # scolded by a warning box is poor feedback.
        self._btn_remove_mcp.setEnabled(False)
        self._btn_remove_mcp.clicked.connect(self._on_remove_mcp_clicked)
        add_row.addWidget(self._btn_add_mcp)
        add_row.addWidget(self._btn_remove_mcp)
        add_row.addStretch(1)
        outer.addLayout(add_row)

        # Empty state — a blank matrix with only role headers looks like a
        # failed load; say plainly there are no MCPs yet.
        self._mcp_empty = QLabel(
            "ยังไม่มี MCP server ใน master registry — กด “+ เพิ่ม MCP…” เพื่อเพิ่ม",
            tab,
        )
        self._mcp_empty.setObjectName("emptyState")
        self._mcp_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mcp_empty.setWordWrap(True)
        self._mcp_empty.hide()
        outer.addWidget(self._mcp_empty)

        self._mcp_table = QTableWidget(tab)
        outer.addWidget(self._mcp_table)
        # Right-click a column header → "Remove MCP <name>" (binds the
        # destructive command to the MCP name; more discoverable than the
        # select-then-button flow).
        hh = self._mcp_table.horizontalHeader()
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._on_mcp_header_menu)
        self._mcp_table.selectionModel().selectionChanged.connect(self._sync_remove_mcp_enabled)
        self._reload_mcp_table()
        return tab

    def _sync_remove_mcp_enabled(self, *_args) -> None:
        cols = {idx.column() for idx in self._mcp_table.selectedIndexes()}
        self._btn_remove_mcp.setEnabled(bool(cols))

    def _on_mcp_header_menu(self, pos) -> None:
        header = self._mcp_table.horizontalHeader()
        col = header.logicalIndexAt(pos)
        if col < 0:
            return
        header_item = self._mcp_table.horizontalHeaderItem(col)
        if header_item is None:
            return
        name = header_item.text()
        menu = QMenu(self)
        act_remove = menu.addAction(f"ลบ MCP '{name}'")
        chosen = menu.exec(header.mapToGlobal(pos))
        if chosen is act_remove:
            self._remove_mcps([name])

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
        self._mcp_table.setVisible(bool(items))
        self._mcp_empty.setVisible(not items)

    def _build_plugin_tab(self) -> QWidget:
        tab = QWidget(self)
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        note = QLabel(
            "security-guidance และ remember ถูก denylist ปิดเสมอทุก pane "
            "(hook หนัก ทำ spawn ช้า) — policy นี้เปิดให้ไม่ได้.",
            tab,
        )
        note.setObjectName("toolsSubtitle")
        note.setWordWrap(True)
        outer.addWidget(note)

        self._plugin_empty = QLabel(
            "ไม่พบ marketplace plugin — ยังไม่มีอะไรใน ~/.claude/plugins/installed_plugins.json",
            tab,
        )
        self._plugin_empty.setObjectName("emptyState")
        self._plugin_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plugin_empty.setWordWrap(True)
        self._plugin_empty.hide()
        outer.addWidget(self._plugin_empty)

        self._plugin_table = QTableWidget(tab)
        outer.addWidget(self._plugin_table)
        self._reload_plugin_table()
        return tab

    def _reload_plugin_table(self) -> None:
        # Marketplace-granular columns (NOT name@marketplace): the policy stores
        # marketplace names, so only these make a checkbox's identity match what
        # is read back — otherwise the grid renders all-unchecked and Save wipes
        # every role. security-guidance/remember have no column of their own (they
        # live inside claude-plugins-official); the pane loader force-denies them
        # regardless, as the tab's note explains.
        items = discover_marketplaces()
        full_orig = self._policy_role_items("plugins")
        rendered = set(items)
        # A role's built-in default can include a marketplace that isn't installed
        # yet (e.g. ui-ux-pro-max-skill scoped to design roles) → it has no column
        # here. Compare + render only marketplaces that HAVE a column, and stash
        # the rest so a Save never silently drops a default that just isn't
        # installed (otherwise a no-op Save writes a lossy override — issue caught
        # by test_plugin_matrix_renders_marketplace_defaults_checked).
        self._hidden_plugin_defaults = {
            r: [m for m in v if m not in rendered] for r, v in full_orig.items()
        }
        self._orig_plugin_items = {r: [m for m in v if m in rendered] for r, v in full_orig.items()}
        matrix = build_matrix(ROLES, items, self._orig_plugin_items)
        self._plugin_boxes = self._fill_matrix_table(self._plugin_table, items, matrix)
        self._plugin_table.setVisible(bool(items))
        self._plugin_empty.setVisible(not items)

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
        table.setVerticalHeaderLabels([role_label(r) for r in ROLES])
        # Full package/plugin names on hover — headers elide when there are
        # many columns.
        for col, item in enumerate(items):
            header_item = table.horizontalHeaderItem(col)
            if header_item is not None:
                header_item.setToolTip(item)

        # Presentation: the cells hold checkbox widgets, not editable text, and
        # the only meaningful selection is a whole column (→ "remove MCP"). Rows
        # alternate-shade with a thin divider (grid off) for a calmer surface;
        # a header click selects the column.
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectColumns)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setCornerButtonEnabled(False)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        vheader = table.verticalHeader()
        vheader.setDefaultSectionSize(38)
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vheader.setMinimumWidth(96)

        hheader = table.horizontalHeader()
        hheader.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hheader.setMinimumSectionSize(104)
        hheader.setHighlightSections(False)
        vheader.setHighlightSections(False)
        # Few columns → equal-stretch so the checkboxes distribute evenly and
        # the grid fills the panel (looks balanced). Many columns → size to the
        # (min-width-clamped) content and let the view scroll horizontally, so
        # long package names stay readable instead of being squeezed to elision.
        if items:
            hheader.setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch
                if len(items) <= 6
                else QHeaderView.ResizeMode.ResizeToContents
            )

        boxes: dict[str, dict[str, QCheckBox]] = {}
        for row, role in enumerate(ROLES):
            boxes[role] = {}
            for col, item in enumerate(items):
                box = QCheckBox(table)
                box.setChecked(matrix.get(role, {}).get(item, False))
                if item in disabled_items:
                    # Blocked by the denylist — render UNCHECKED + disabled. A
                    # checked-but-locked box would read as "enabled", the exact
                    # opposite of "policy can never turn this on".
                    box.setChecked(False)
                    box.setEnabled(False)
                    box.setToolTip("denylist ปิดเสมอ — เปิดผ่าน policy ไม่ได้")
                table.setCellWidget(row, col, _CheckCell(box, table))
                boxes[role][item] = box
        return boxes

    # ── add/remove MCP ──────────────────────────────────────────

    def _on_add_mcp_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("เพิ่ม MCP")
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        form.setContentsMargins(18, 18, 18, 14)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
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
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setObjectName("primaryBtn")
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self._remove_mcps(names)

    def _remove_mcps(self, names: list[str]) -> None:
        """Confirm + drop *names* from the master MCP registry, then reload."""
        if not names:
            return
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

            # Write BOTH kinds for every role that changed EITHER. set_role_items
            # seeds a *fresh* role entry's sibling kind to [] (an explicit deny
            # override), so persisting a plugins-only change would silently wipe
            # that role's MCPs — the exact bug that stripped playwright +
            # chrome-devtools from qa/critic/designer and left QA unable to drive
            # a browser. The matrix already holds the correct value for BOTH
            # kinds (built from effective defaults), so writing both from the
            # matrix keeps the untouched kind at its real value instead of an
            # accidental deny. Redundant when a role's kind equals its default,
            # but never wrong; a silent deny is far worse than a redundant entry.
            hidden = getattr(self, "_hidden_plugin_defaults", {})
            for role in set(mcp_changes) | set(plugin_changes):
                pane_tools_policy.set_role_items(role, "mcps", updated_mcps[role])
                # Re-add default marketplaces that have no column (not installed)
                # so a scoped role (e.g. design → ui-ux-pro-max-skill) keeps them.
                pane_tools_policy.set_role_items(
                    role, "plugins", updated_plugins[role] + hidden.get(role, [])
                )
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
