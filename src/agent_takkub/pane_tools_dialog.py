"""PaneToolsDialog — per-role MCP/plugin policy editor + Team & Roles manager.

Three-tab QDialog: a role x MCP checkbox matrix, a role x plugin checkbox
matrix, and a "Team & Roles" tab (team list + guided 3-step custom-role
create, A6-redesign). Backed by ``pane_tools_policy`` (persisted MCP/plugin
policy), ``shared_dev_tools`` (master MCP registry + role-variant
regeneration), and ``custom_roles`` (A6 custom-role registry).

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
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import custom_roles

# A6 (Role & Skill Manager): pure logic used by the Team & Roles tab (guided
# create) is kept in `custom_roles` / `skill_audit` (no Qt), so it's testable
# without a display — this module only wires it to widgets.

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
    "analyst",
    "security",
    "docs",
)

# Tab indices, exposed so callers (status_header/user_actions) can open the
# dialog straight to a specific tab instead of always landing on MCP.
TAB_MCP, TAB_PLUGINS, TAB_TEAM = 0, 1, 2

# Short "what does this do" blurbs for the Step-2 tool cards (A6-redesign) — a
# bare checkbox next to a package name makes the user guess; these are just
# enough to decide without leaving the dialog. Unknown tools (a freshly added
# MCP, a marketplace this map hasn't caught up with) fall back to a generic
# label rather than showing nothing.
_TOOL_HINTS: dict[str, str] = {
    "playwright": "เปิด browser เทส",
    "chrome-devtools": "debug เว็บ",
    "obsidian-vault": "อ่าน/เขียน vault บันทึก",
    "context7": "ดึง doc library ล่าสุด",
    "superpowers-dev": "skill library เสริม",
    "addy-agent-skills": "skill เสริมจาก addy",
    "pordee": "workflow เฉพาะทีม",
    "claude-plugins-official": "ปลั๊กอินทางการ Anthropic",
    "ui-ux-pro-max-skill": "AI ช่วยออกแบบ UI/UX",
}


def tool_hint(name: str) -> str:
    """Short description for a Step-2 tool card. Unknown name -> generic label."""
    return _TOOL_HINTS.get(name, "เครื่องมือเสริม")


def _default_plugins_installed_file() -> pathlib.Path:
    """``<config.default_claude_config_dir()>/plugins/installed_plugins.json``
    — plain ``~/.claude`` for a dev checkout, the isolated per-instance profile
    for an installed build. Resolved fresh (not a module constant) so it tracks
    ``config.DATA_HOME`` / ``Path.home`` even when a test monkeypatches them
    after import."""
    from .config import default_claude_config_dir

    return default_claude_config_dir() / "plugins" / "installed_plugins.json"


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

/* Team & Roles tab (A6-redesign) — QComboBox/QSpinBox/QPlainTextEdit have no
   dark styling by default (plain-white background), unlike QLineEdit above
   which the rest of this dialog relies on — without this block they'd
   render as a jarring white patch. */
QComboBox, QSpinBox, QPlainTextEdit {
    background: #18181b; border: 1px solid #27272a; border-radius: 6px;
    padding: 4px 8px; color: #e4e4e7; selection-background-color: #2563eb;
}
QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #2563eb; }
QComboBox QAbstractItemView {
    background: #18181b; color: #e4e4e7; border: 1px solid #27272a;
    selection-background-color: #2563eb;
}
QSpinBox::up-button, QSpinBox::down-button { background: #27272a; border: none; width: 16px; }

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
        from .config import SETTINGS_HOME

        cache = SETTINGS_HOME / "cache"
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
    installed_file: pathlib.Path | None = None,
) -> list[str]:
    """Plugin names (``name@marketplace``) known to this machine's Claude
    plugin install registry. Missing/unreadable file -> empty list (dialog
    just shows no plugin columns, doesn't crash)."""
    if installed_file is None:
        installed_file = _default_plugins_installed_file()
    try:
        data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return []
    return sorted(plugins.keys())


def master_mcps() -> list[str]:
    """Master MCP registry names (``shared_dev_tools.list_master_mcps()``),
    empty on any failure. Module-level so non-Qt callers (settings_window's
    native MCP Matrix view) can build a matrix without instantiating
    ``PaneToolsDialog``."""
    try:
        from . import shared_dev_tools

        return list(shared_dev_tools.list_master_mcps())
    except Exception:
        return []


def policy_role_items(roles: tuple[str, ...], kind: str) -> dict[str, list[str]]:
    """Current per-role item names for *kind* ('mcps'/'plugins'), resolved
    through ``pane_tools_policy`` defaults. Module-level so it's shared
    between ``PaneToolsDialog`` and settings_window's native matrix views
    instead of being duplicated."""
    try:
        from . import pane_tools_policy, shared_dev_tools
        from .lead_context import _ROLE_PLUGIN_POLICY, _TEAMMATE_PLUGINS

        defaults = getattr(shared_dev_tools, "_ROLE_MCP_POLICY", {})
        result: dict[str, list[str]] = {}
        for role in roles:
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
        return {role: [] for role in roles}


def discover_marketplaces(
    installed_file: pathlib.Path | None = None,
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

    if installed_file is None:
        installed_file = _default_plugins_installed_file()
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


class _ToolCard(QWidget):
    """Step-2 guided-create tool toggle: a clickable card with a title + a
    one-line hint, not a bare checkbox — the A6-redesign problem was that a
    checkbox next to a raw package name ("chrome-devtools") gives the user
    nothing to decide on. Same whole-card-toggles pattern as ``_CheckCell``.
    """

    def __init__(self, name: str, hint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.box = QCheckBox(self)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 8, 9, 8)
        lay.setSpacing(8)
        lay.addWidget(self.box, alignment=Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title = QLabel(name, self)
        title.setStyleSheet("font-size: 12px; font-weight: 700; color: #e4e4e7;")
        desc = QLabel(hint, self)
        desc.setStyleSheet("font-size: 11px; color: #a1a1aa;")
        desc.setWordWrap(True)
        text_col.addWidget(title)
        text_col.addWidget(desc)
        lay.addLayout(text_col, 1)

        self.box.toggled.connect(self._apply_style)
        self._apply_style(False)

    def isChecked(self) -> bool:
        return self.box.isChecked()

    def setChecked(self, value: bool) -> None:
        self.box.setChecked(value)

    def mousePressEvent(self, ev) -> None:
        self.box.toggle()
        super().mousePressEvent(ev)

    def _apply_style(self, checked: bool) -> None:
        if checked:
            self.setStyleSheet(
                "QWidget#toolCard { border: 1.5px solid #6366f1; border-radius: 10px;"
                " background: rgba(99,102,241,0.14); }"
            )
        else:
            self.setStyleSheet(
                "QWidget#toolCard { border: 1.5px solid #27272a; border-radius: 10px;"
                " background: #1a1a1e; }"
            )


class PaneToolsDialog(QDialog):
    """Settings dialog for per-role MCP + plugin visibility, plus the A6-redesign
    "Team & Roles" tab (team list + guided 3-step custom-role create).

    Loads the current policy on open, lets the user tick/untick per-role
    checkboxes, and only writes back (``pane_tools_policy.save_policy`` +
    ``shared_dev_tools.regen_role_variants``) when Save is clicked.
    """

    def __init__(self, parent: QWidget | None = None, initial_tab: int = TAB_MCP) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pane Tools — MCP, Plugins & Team")
        self.resize(920, 600)
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

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_mcp_tab(), "MCP")
        self._tabs.addTab(self._build_plugin_tab(), "Plugins")
        self._tabs.addTab(self._build_team_roles_tab(), "👥 Team & Roles")
        self._tabs.setCurrentIndex(initial_tab)

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

    # ── responsive tool-card grids (guided-create step 2) ──────────
    # 2 columns fits the dialog's default width; below 600px a 2nd column
    # leaves each card too narrow for its name+hint and they clip/overlap.

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adjust_grids()

    def _adjust_grids(self) -> None:
        # Guard: __init__ calls self.resize() before the Team & Roles tab
        # (and its grids) exist — a resizeEvent that lands in that window
        # must no-op instead of raising.
        if not hasattr(self, "_nr_mcp_grid"):
            return
        cols = 1 if self.width() < 600 else 2
        self._regrid_cards(self._nr_mcp_grid, self._nr_mcp_cards, cols)
        self._regrid_cards(self._nr_plugin_grid, self._nr_plugin_cards, cols)

    @staticmethod
    def _regrid_cards(grid: QGridLayout, cards: list[_ToolCard], target_cols: int) -> None:
        # Detach before re-adding — QGridLayout can leave a stale cell entry
        # behind if a widget is re-added to a different cell without first
        # being removed from its current one.
        for card in cards:
            grid.removeWidget(card)
        for idx, card in enumerate(cards):
            grid.addWidget(card, idx // target_cols, idx % target_cols)

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
        return master_mcps()

    def _policy_role_items(self, kind: str) -> dict[str, list[str]]:
        """Current per-role item names for *kind* ('mcps'/'plugins'),
        resolved through ``pane_tools_policy`` defaults."""
        return policy_role_items(ROLES, kind)

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

    # ── Team & Roles tab (A6-redesign) ──────────────────────────

    def _build_team_roles_tab(self) -> QWidget:
        """Two columns: a team list (left) + a guided 3-step custom-role
        create form with live preview (right). Replaces the old flat
        "+ New Role" form (name/color/tools/instructions all in one
        undifferentiated block, which is what made users unsure what to fill
        in first) and the separate "Skill Catalog" browsing tab (folded into
        the live overlap-warning in step 3 — nobody actually browsed the full
        corpus, they wanted to know "does MY new role step on anything").
        """
        tab = QWidget(self)
        outer = QHBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # LEFT — team list
        left = QWidget(tab)
        left.setFixedWidth(240)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(10, 12, 6, 10)
        left_lay.setSpacing(6)

        list_scroll = QScrollArea(left)
        list_scroll.setWidgetResizable(True)
        list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._team_list_container = QWidget()
        self._team_list_layout = QVBoxLayout(self._team_list_container)
        self._team_list_layout.setContentsMargins(0, 0, 0, 0)
        self._team_list_layout.setSpacing(2)
        list_scroll.setWidget(self._team_list_container)
        left_lay.addWidget(list_scroll, 1)

        self._btn_add_role = QPushButton("＋ สร้าง role ใหม่", left)
        self._btn_add_role.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_role.clicked.connect(self._on_reset_new_role_form)
        left_lay.addWidget(self._btn_add_role)

        # "Users / บัญชี" section: a live summary of registered Claude user
        # profiles + a button into the existing Add/Remove-user dialog
        # (moved here from the 👥 Team chip's right-click-only menu per user
        # request — same dialog, reused via `open_user_profiles_dialog`, not
        # rebuilt here, so add/remove/Claude-Auth logic has one home).
        users_sep = QFrame(left)
        users_sep.setFrameShape(QFrame.Shape.HLine)
        users_sep.setStyleSheet("color: #27272a;")
        left_lay.addWidget(users_sep)

        users_label = QLabel("👤 Users / บัญชี", left)
        users_label.setObjectName("toolsSubtitle")
        left_lay.addWidget(users_label)

        self._users_summary = QLabel("", left)
        self._users_summary.setWordWrap(True)
        self._users_summary.setStyleSheet("color: #a1a1aa; font-size: 11px;")
        left_lay.addWidget(self._users_summary)

        self._btn_manage_users = QPushButton("จัดการบัญชี…", left)
        self._btn_manage_users.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_manage_users.setToolTip("เพิ่ม/ลบ user profile + ตั้งค่า Claude Auth ต่อ profile")
        self._btn_manage_users.clicked.connect(self._on_manage_users_clicked)
        left_lay.addWidget(self._btn_manage_users)

        outer.addWidget(left)

        divider = QFrame(tab)
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color: #27272a;")
        outer.addWidget(divider)

        # RIGHT — guided create, scrollable (steps + tool grid can outgrow a
        # small screen's dialog height).
        right_scroll = QScrollArea(tab)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right = QWidget()
        outer_r = QVBoxLayout(right)
        outer_r.setContentsMargins(16, 14, 16, 14)
        outer_r.setSpacing(14)

        create_title = QLabel("✨ สร้าง role ใหม่ · 3 ขั้น", right)
        create_title.setObjectName("toolsTitle")
        create_title.setStyleSheet("font-size: 14px;")
        outer_r.addWidget(create_title)

        # Step 1 — identity. Default avatar color auto-follows the typed name
        # (same hash-to-palette function project_nav uses for project
        # avatars) until the user overrides it via a swatch/custom pick.
        step1 = QLabel("① role นี้ชื่ออะไร", right)
        step1.setStyleSheet("font-weight: 700; font-size: 13px;")
        outer_r.addWidget(step1)
        hint1 = QLabel("ชื่อสั้นๆ ตัวเล็ก ใช้สั่งงานผ่าน --role · avatar สร้างสีให้อัตโนมัติ", right)
        hint1.setObjectName("toolsSubtitle")
        outer_r.addWidget(hint1)

        name_row = QHBoxLayout()
        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("ชื่อ (สั่งงาน)", right))
        self._nr_name = QLineEdit(right)
        self._nr_name.setPlaceholderText("data-eng (a-z0-9-_ เท่านั้น, ห้ามชนกับ role เดิม)")
        self._nr_name.textChanged.connect(self._on_new_role_changed)
        name_col.addWidget(self._nr_name)
        label_col = QVBoxLayout()
        label_col.addWidget(QLabel("ป้าย + อีโมจิ", right))
        self._nr_label = QLineEdit(right)
        self._nr_label.setPlaceholderText("🧬 Data Eng")
        self._nr_label.textChanged.connect(self._update_preview)
        label_col.addWidget(self._nr_label)
        name_row.addLayout(name_col, 1)
        name_row.addLayout(label_col, 1)
        outer_r.addLayout(name_row)

        from . import project_nav

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        self._nr_color = "#94a3b8"
        self._nr_color_touched = False
        self._nr_swatch_btns: list[QPushButton] = []
        for color in project_nav._AVATAR_COLORS:
            sw = QPushButton("", right)
            sw.setFixedSize(22, 22)
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.clicked.connect(lambda _checked=False, c=color: self._on_swatch_clicked(c))
            self._nr_swatch_btns.append(sw)
            swatch_row.addWidget(sw)
        self._nr_color_btn = QPushButton("…", right)
        self._nr_color_btn.setFixedSize(22, 22)
        self._nr_color_btn.setToolTip("เลือกสีเอง")
        self._nr_color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nr_color_btn.clicked.connect(self._on_pick_color)
        swatch_row.addWidget(self._nr_color_btn)
        swatch_row.addStretch(1)
        outer_r.addLayout(swatch_row)
        self._update_color_swatch()

        # Step 2 — tools: toggle CARDS (title + one-line "what this does"
        # hint), not bare checkboxes next to a raw package name.
        step2 = QLabel("② ให้ใช้เครื่องมืออะไรได้", right)
        step2.setStyleSheet("font-weight: 700; font-size: 13px;")
        outer_r.addWidget(step2)
        hint2 = QLabel("เลือกเฉพาะที่จำเป็น — default ไม่มี MCP เพื่อประหยัด token", right)
        hint2.setObjectName("toolsSubtitle")
        outer_r.addWidget(hint2)

        mcp_label = QLabel("MCP", right)
        mcp_label.setObjectName("toolsSubtitle")
        outer_r.addWidget(mcp_label)
        self._nr_mcp_grid = QGridLayout()
        self._nr_mcp_grid.setSpacing(8)
        self._nr_mcp_boxes: dict[str, QCheckBox] = {}
        self._nr_mcp_cards: list[_ToolCard] = []
        for name in self._master_mcps():
            card = _ToolCard(name, tool_hint(name), right)
            self._nr_mcp_boxes[name] = card.box
            self._nr_mcp_cards.append(card)
        outer_r.addLayout(self._nr_mcp_grid)

        plugin_label = QLabel("Plugins", right)
        plugin_label.setObjectName("toolsSubtitle")
        outer_r.addWidget(plugin_label)
        self._nr_plugin_grid = QGridLayout()
        self._nr_plugin_grid.setSpacing(8)
        self._nr_plugin_boxes: dict[str, QCheckBox] = {}
        self._nr_plugin_cards: list[_ToolCard] = []
        for name in discover_marketplaces():
            card = _ToolCard(name, tool_hint(name), right)
            self._nr_plugin_boxes[name] = card.box
            self._nr_plugin_cards.append(card)
        outer_r.addLayout(self._nr_plugin_grid)
        self._regrid_cards(self._nr_mcp_grid, self._nr_mcp_cards, 2)
        self._regrid_cards(self._nr_plugin_grid, self._nr_plugin_cards, 2)

        # Step 3 — habits (optional): instructions + an "advanced" collapsible
        # that hides fields most users never touch (grid position,
        # provider/model) so they don't compete with the ones that matter.
        step3 = QLabel("③ role นี้ถนัดอะไร (ไม่บังคับ)", right)
        step3.setStyleSheet("font-weight: 700; font-size: 13px;")
        outer_r.addWidget(step3)
        hint3 = QLabel("เขียนสั้นๆ — ปล่อยว่างได้ ระบบเติม template ให้", right)
        hint3.setObjectName("toolsSubtitle")
        outer_r.addWidget(hint3)

        self._nr_instructions = QPlainTextEdit(right)
        self._nr_instructions.setPlaceholderText(
            "บอก role ตัวเองว่าทำหน้าที่อะไร ขอบเขตงานคืออะไร รายงานยังไง..."
        )
        self._nr_instructions.setMinimumHeight(80)
        self._nr_instructions.textChanged.connect(self._on_new_role_changed)
        outer_r.addWidget(self._nr_instructions)

        self._nr_advanced_toggle = QPushButton("▸ ตั้งค่าขั้นสูง", right)
        self._nr_advanced_toggle.setFlat(True)
        self._nr_advanced_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nr_advanced_toggle.setStyleSheet(
            "text-align: left; color: #71717a; font-family: monospace;"
            " border: none; padding: 2px 0;"
        )
        self._nr_advanced_toggle.clicked.connect(self._on_toggle_advanced)
        outer_r.addWidget(self._nr_advanced_toggle)

        self._nr_advanced_body = QWidget(right)
        adv_form = QFormLayout(self._nr_advanced_body)
        adv_form.setHorizontalSpacing(14)
        adv_form.setVerticalSpacing(8)
        self._nr_column = QComboBox(right)
        self._nr_column.addItem("1 · Dev column (ใต้ codex)", 1)
        self._nr_column.addItem("2 · Support column (ใต้ shell)", 2)
        self._nr_column.setCurrentIndex(1)
        adv_form.addRow("Grid column", self._nr_column)
        self._nr_row = QSpinBox(right)
        self._nr_row.setRange(0, 99)
        self._nr_row.setValue(99)
        self._nr_row.setToolTip(
            "แถวในกริด — ถ้าซ้ำกับ role อื่นในคอลัมน์เดียวกัน จะซ้อนทับกันในหน้าจอ "
            "(ยังไม่มี auto-collision — ปรับเลขเองถ้าไม่ต้องการซ้อน)"
        )
        adv_form.addRow("Grid row", self._nr_row)
        # #103: custom-role provider isn't wired yet — every custom role
        # spawns on Claude regardless of what's picked here. Shown-but-disabled
        # (not hidden) so the field is discoverable, not a surprise later.
        self._nr_provider_combo = QComboBox(right)
        self._nr_provider_combo.addItem("Claude (default)")
        self._nr_provider_combo.setEnabled(False)
        self._nr_provider_combo.setToolTip(
            "รองรับ provider อื่น (codex/gemini) เร็วๆ นี้ — ตอนนี้ custom role รันบน Claude เท่านั้น (#103)"
        )
        adv_form.addRow("Provider/model", self._nr_provider_combo)
        self._nr_advanced_body.hide()
        outer_r.addWidget(self._nr_advanced_body)

        self._nr_overlap_label = QLabel("", right)
        self._nr_overlap_label.setObjectName("toolsSubtitle")
        self._nr_overlap_label.setWordWrap(True)
        outer_r.addWidget(self._nr_overlap_label)

        # Live preview — the exact avatar + `takkub assign` invocation Create
        # is about to produce, before committing.
        preview_box = QFrame(right)
        preview_box.setFrameShape(QFrame.Shape.StyledPanel)
        preview_box.setStyleSheet(
            "QFrame { border: 1px solid #27272a; border-radius: 8px; background: #0c0c0e; }"
        )
        preview_lay = QVBoxLayout(preview_box)
        preview_lay.setContentsMargins(12, 10, 12, 10)
        preview_head = QLabel("พรีวิว — role นี้จะเป็นแบบนี้", preview_box)
        preview_head.setObjectName("toolsSubtitle")
        preview_lay.addWidget(preview_head)
        chip_row = QHBoxLayout()
        self._nr_preview_avatar = QLabel("?", preview_box)
        self._nr_preview_avatar.setFixedSize(26, 26)
        self._nr_preview_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip_row.addWidget(self._nr_preview_avatar)
        self._nr_preview_label = QLabel("", preview_box)
        self._nr_preview_label.setStyleSheet("font-weight: 700; font-size: 13px;")
        chip_row.addWidget(self._nr_preview_label)
        chip_row.addStretch(1)
        preview_lay.addLayout(chip_row)
        self._nr_preview_cmd = QLabel("", preview_box)
        self._nr_preview_cmd.setStyleSheet(
            "font-family: monospace; font-size: 11px; color: #a1a1aa;"
        )
        self._nr_preview_cmd.setWordWrap(True)
        preview_lay.addWidget(self._nr_preview_cmd)
        outer_r.addWidget(preview_box)

        create_row = QHBoxLayout()
        self._nr_create_btn = QPushButton("สร้าง role", right)
        self._nr_create_btn.setObjectName("primaryBtn")
        self._nr_create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nr_create_btn.clicked.connect(self._on_create_role_clicked)
        create_row.addWidget(self._nr_create_btn)
        create_row.addStretch(1)
        outer_r.addLayout(create_row)
        outer_r.addStretch(1)

        right_scroll.setWidget(right)
        outer.addWidget(right_scroll, 1)

        self._reload_team_list()
        self._reload_users_summary()
        self._update_preview()
        return tab

    # ── users / บัญชี (left column) ──────────────────────────────

    def _reload_users_summary(self) -> None:
        from . import user_profile

        names = [p["name"] for p in user_profile.list_profiles()]
        self._users_summary.setText(" · ".join(names) if names else "(default only)")

    def _on_manage_users_clicked(self) -> None:
        from .user_actions import open_user_profiles_dialog

        open_user_profiles_dialog(self, lambda msg: self._status_label.setText(msg))
        self._reload_users_summary()

    # ── team list (left column) ─────────────────────────────────

    def _reload_team_list(self) -> None:
        from . import custom_roles as cr
        from . import roles as roles_mod

        layout = self._team_list_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        built_header = QLabel("Built-in · ลบไม่ได้", self._team_list_container)
        built_header.setObjectName("toolsSubtitle")
        layout.addWidget(built_header)
        for role in roles_mod.ALL_DEFAULT:
            layout.addWidget(self._build_role_row(role.name, role.label, removable=False))

        custom = cr.load_custom_roles()
        if custom:
            custom_header = QLabel("Custom · ของคุณ", self._team_list_container)
            custom_header.setObjectName("toolsSubtitle")
            layout.addWidget(custom_header)
            for name in sorted(custom):
                r = custom[name]
                layout.addWidget(self._build_role_row(r.name, r.label, removable=True))

        layout.addStretch(1)

    def _build_role_row(self, name: str, label: str, removable: bool) -> QWidget:
        from . import project_nav

        row = QWidget(self._team_list_container)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        avatar = QLabel(project_nav._initials(label), row)
        avatar.setFixedSize(26, 26)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {project_nav._avatar_color(name)}; color: #fff;"
            f" font-weight: 800; font-size: 10px; border-radius: 13px;"
        )
        lay.addWidget(avatar)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        name_lbl = QLabel(label, row)
        name_lbl.setStyleSheet("font-size: 12.5px; font-weight: 600; color: #e4e4e7;")
        mono_lbl = QLabel(name, row)
        mono_lbl.setStyleSheet("font-family: monospace; font-size: 10px; color: #71717a;")
        text_col.addWidget(name_lbl)
        text_col.addWidget(mono_lbl)
        lay.addLayout(text_col, 1)

        if removable:
            rm = QPushButton("✕", row)
            rm.setFixedSize(22, 22)
            rm.setCursor(Qt.CursorShape.PointingHandCursor)
            rm.setToolTip(f"ลบ role '{name}'")
            rm.clicked.connect(lambda _checked=False, n=name: self._on_delete_role_clicked(n))
            lay.addWidget(rm)
        else:
            lock = QLabel("🔒", row)
            lock.setToolTip("built-in — ลบไม่ได้")
            lock.setStyleSheet("color: #3f3f46;")
            lay.addWidget(lock)

        return row

    def _on_delete_role_clicked(self, name: str) -> None:
        confirm = QMessageBox.question(
            self,
            "ลบ role",
            f"ลบ role '{name}' ออกจากทีม? (spawn ด้วย --role {name} จะใช้ไม่ได้อีก — "
            "ไฟล์ instructions เดิมยังอยู่ ไม่ถูกลบ)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        from . import custom_roles as cr
        from . import roles as roles_mod

        if not cr.delete_role(name):
            QMessageBox.warning(self, "ลบไม่สำเร็จ", "เขียน custom-roles.json ไม่สำเร็จ")
            return
        roles_mod.unregister_role(name)
        self._status_label.setText(f"ลบ role '{name}' แล้ว")
        self._reload_team_list()

    # ── guided create: color / preview / overlap / advanced ──────

    def _update_color_swatch(self) -> None:
        for sw, color in zip(self._nr_swatch_btns, self._avatar_palette(), strict=False):
            ring = (
                "2px solid #fafafa"
                if color.lower() == self._nr_color.lower()
                else "2px solid transparent"
            )
            sw.setStyleSheet(f"background-color: {color}; border-radius: 11px; border: {ring};")
        self._nr_color_btn.setStyleSheet(
            f"background-color: {self._nr_color}; border: 1px solid #3f3f46; border-radius: 11px;"
        )
        self._nr_color_btn.setToolTip(f"สีปัจจุบัน: {self._nr_color} — คลิกเพื่อเลือกเอง")

    @staticmethod
    def _avatar_palette() -> tuple[str, ...]:
        from . import project_nav

        return project_nav._AVATAR_COLORS

    def _on_swatch_clicked(self, color: str) -> None:
        self._nr_color = color
        self._nr_color_touched = True
        self._update_color_swatch()
        self._update_preview()

    def _on_pick_color(self) -> None:
        from PyQt6.QtGui import QColor

        picked = QColorDialog.getColor(QColor(self._nr_color), self, "เลือกสี Role")
        if not picked.isValid():
            return
        self._nr_color = picked.name()
        self._nr_color_touched = True
        self._update_color_swatch()
        self._update_preview()

    def _on_toggle_advanced(self) -> None:
        # isHidden() reflects the widget's OWN explicit hide()/setVisible()
        # flag; isVisible() also folds in ancestor-chain visibility, which is
        # always False before the dialog's first exec()/show() — using it
        # here would make this only ever expand, never collapse, pre-show.
        hidden = self._nr_advanced_body.isHidden()
        self._nr_advanced_body.setVisible(hidden)
        self._nr_advanced_toggle.setText("▾ ตั้งค่าขั้นสูง" if hidden else "▸ ตั้งค่าขั้นสูง")

    def _update_preview(self, *_args) -> None:
        from . import project_nav

        name = self._nr_name.text().strip().lower()
        label = self._nr_label.text().strip() or (name.capitalize() if name else "role")
        self._nr_preview_avatar.setStyleSheet(
            f"background: {self._nr_color}; color: #fff; font-weight: 800;"
            " font-size: 10px; border-radius: 13px;"
        )
        self._nr_preview_avatar.setText(project_nav._initials(label))
        self._nr_preview_label.setText(label if name else "—")
        self._nr_preview_cmd.setText(f'takkub assign --role {name or "<name>"} "..."')

    def _on_new_role_changed(self, *_args) -> None:
        """Live auto-color + overlap warning as the user types a
        name/instructions — checked BEFORE Create so a near-duplicate role is
        caught early."""
        from . import project_nav

        name = self._nr_name.text().strip().lower()
        if name and not self._nr_color_touched:
            self._nr_color = project_nav._avatar_color(name)
            self._update_color_swatch()
        self._update_preview()

        if not name:
            self._nr_overlap_label.setText("")
            return
        ok, err = custom_roles.validate_role_name(name)
        if not ok:
            self._nr_overlap_label.setText(f"⚠️ {err}")
            return
        text = self._nr_instructions.toPlainText().strip()
        if not text:
            self._nr_overlap_label.setText("")
            return
        from . import skill_audit

        overlaps = skill_audit.audit_new_role_text(name, text)
        if overlaps:
            parts = ", ".join(f"{r} ({sim:.0%})" for r, sim in overlaps)
            self._nr_overlap_label.setText(f"💡 คล้าย {parts} นิดๆ — ตั้งใจแยก ok เลย")
        else:
            self._nr_overlap_label.setText("✓ ไม่ทับซ้อนกับ role อื่น")

    def _on_reset_new_role_form(self) -> None:
        self._reset_new_role_form()
        self._nr_name.setFocus()

    def _reset_new_role_form(self) -> None:
        self._nr_name.clear()
        self._nr_label.clear()
        self._nr_instructions.clear()
        for cb in {**self._nr_mcp_boxes, **self._nr_plugin_boxes}.values():
            cb.setChecked(False)
        self._nr_column.setCurrentIndex(1)
        self._nr_row.setValue(99)
        self._nr_color = "#94a3b8"
        self._nr_color_touched = False
        self._nr_advanced_body.hide()
        self._nr_advanced_toggle.setText("▸ ตั้งค่าขั้นสูง")
        self._update_color_swatch()
        self._nr_overlap_label.setText("")
        self._update_preview()

    def _on_create_role_clicked(self) -> None:
        name = self._nr_name.text().strip().lower()
        label = self._nr_label.text().strip()
        column = self._nr_column.currentData()
        row = self._nr_row.value()
        instructions = self._nr_instructions.toPlainText().strip() or None

        ok, err = custom_roles.create_role(name, label, self._nr_color, column, row, instructions)
        if not ok:
            QMessageBox.warning(self, "สร้าง Role ไม่สำเร็จ", err)
            return

        # Register in THIS process immediately so `--role <name>` spawns
        # without waiting for a cockpit restart (roles.py otherwise only
        # loads custom-roles.json at boot).
        from . import roles as roles_mod

        role = custom_roles.load_custom_roles().get(name)
        if role is not None:
            roles_mod.register_role(role)

        mcp_names = [n for n, cb in self._nr_mcp_boxes.items() if cb.isChecked()]
        plugin_names = [n for n, cb in self._nr_plugin_boxes.items() if cb.isChecked()]
        try:
            from . import pane_tools_policy

            pane_tools_policy.set_role_items(name, "mcps", mcp_names)
            pane_tools_policy.set_role_items(name, "plugins", plugin_names)
        except Exception as e:
            QMessageBox.warning(self, "สร้าง role สำเร็จ แต่ตั้ง tools policy ไม่สำเร็จ", str(e))

        self._status_label.setText(
            f"สร้าง role '{name}' แล้ว — spawn ได้ทันทีด้วย "
            f'`takkub assign --role {name} "..."` (ไม่ต้อง restart cockpit)'
        )
        self._reset_new_role_form()
        self._reload_team_list()

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
