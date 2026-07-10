"""Task Tree dock (A8): a read-only, right-hand `QDockWidget` panel that
renders the Task Ledger (A7) as a live tree — one collapsible card per
project, then 🎯 goal → feature → task rows with a status badge.

Reads `task_ledger.load_state()` (the JSON sidecar A7 already writes on every
assign/done/fail/close) directly — never parses `INDEX.md` markdown, so the
tree can't drift from the ledger's own structured view.

Pure helpers (`status_glyph`, `project_progress`, `has_any_rows`) are kept
free of any Qt import so they're unit-testable without a QApplication; only
`TaskDockWidget` itself touches PyQt.

Visuals (A8-polish) match the left PROJECTS sidebar's design language
(`project_nav._SIDEBAR_QSS`/`_ProjectRow`): dark card background, rounded
hover/selected rows, and the same deterministic avatar coloring — reused
directly from `project_nav` (and `token_meter.usage_color` for badge/bar
color) so the same project shows the same avatar tint in both places.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import task_ledger
from .config import list_project_names
from .project_nav import _avatar_color, _initials
from .token_meter import usage_color

# (glyph, hex color) per ledger row status — mirrors task_ledger._ROW_SYMBOL
# but with the dock's own richer glyphs/colors (INDEX.md uses plain ASCII
# checkboxes since markdown can't carry color).
_STATUS_GLYPH = {
    "working": ("◔", "#facc15"),  # ◔ amber
    "ok": ("✓", "#22c55e"),  # ✓ green
    "fail": ("✕", "#ef4444"),  # ✕ red
    "closed": ("➖", "#71717a"),  # ➖ gray
    "superseded": ("›", "#a78bfa"),  # › purple
}
# Any status the ledger doesn't emit yet (e.g. a future "queued" row) falls
# back to an empty checkbox in neutral gray instead of crashing render.
_STATUS_FALLBACK = ("☐", "#71717a")  # ☐

# Extra QTreeWidgetItem data role (column 0): the row's un-prefixed label
# text — goal/feature items re-derive their ▸/▾-prefixed text from this on
# every expand/collapse.
_ROLE_BASE_LABEL = Qt.ItemDataRole.UserRole + 1

_DOCK_QSS = """
#taskDockRoot {
    background: #0e0e10;
}
#taskDockHeader {
    color: #52525b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 6px 6px 4px 6px;
}
QTreeWidget#taskTree {
    background: #0e0e10;
    color: #d4d4d8;
    border: 1px solid #27272a;
    border-radius: 8px;
    outline: 0;
    padding: 4px;
}
QTreeWidget#taskTree::item {
    border-radius: 8px;
    padding: 3px 2px;
    margin: 1px 0;
}
QTreeWidget#taskTree::item:hover {
    background: #18181b;
}
QTreeWidget#taskTree::item:selected {
    background: #1e1b2e;
}
QTreeWidget#taskTree::branch {
    background: transparent;
    border-image: none;
    image: none;
}
"""


def status_glyph(status: str) -> tuple[str, str]:
    """(unicode glyph, hex color) for a ledger row's `status` field."""
    return _STATUS_GLYPH.get(status, _STATUS_FALLBACK)


def project_progress(state: dict) -> tuple[int, int]:
    """(done rows, total rows) across every group/feature in *state*.

    "done" counts `ok` only, matching the `progress: X/Y` line `task_ledger`
    itself renders into INDEX.md — `fail`/`closed`/`superseded` are terminal
    but not successes, `working` is still open.
    """
    total = 0
    done = 0
    for group in state.get("groups", []):
        for feat in group.get("features", []):
            for row in feat.get("rows", []):
                total += 1
                if row.get("status") == "ok":
                    done += 1
    return done, total


def has_any_rows(state: dict) -> bool:
    """True if *state* has at least one task row — an empty/never-assigned
    project's card is skipped instead of rendered blank."""
    return project_progress(state)[1] > 0


def feature_emoji(feature: dict) -> str:
    """Mirrors `task_ledger._feature_emoji` (rows-in-progress → 🔨, any
    failure → ⚠️, all terminal → ✅, empty → ⏳) so the dock and INDEX.md
    never disagree about a feature's at-a-glance state."""
    statuses = {r.get("status") for r in feature.get("rows", [])}
    if not statuses:
        return "⏳"  # ⏳
    if "working" in statuses:
        return "\U0001f528"  # 🔨
    if "fail" in statuses:
        return "⚠️"  # ⚠️
    if statuses <= {"ok", "closed", "superseded"}:
        return "✅"  # ✅
    return "⏳"  # ⏳


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _row_status_icon(glyph: str, color: str) -> QIcon:
    """A small rounded pill icon (tinted circle + glyph) for a task row's
    status column — painted in-memory so it needs no bundled image asset and
    stays crisp on both Windows and macOS. Rendered as a real `QIcon` (not an
    item widget) so it paints through the normal item delegate alongside the
    item's own text — an item widget here would sit over transparent space
    and let the delegate's separately-painted text bleed through underneath,
    doubling the text visually.
    """
    size = 16
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    r, g, b = _hex_to_rgb(color)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(r, g, b, 40))
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor(color))
    font = painter.font()
    font.setPointSize(8)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pix)


class TaskDockWidget(QWidget):
    """Right-dock panel body: a QTreeWidget of every open project's ledger."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("taskDockRoot")
        self.setStyleSheet(_DOCK_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        title = QLabel("TASK LEDGER")
        title.setObjectName("taskDockHeader")
        root.addWidget(title)

        self._tree = QTreeWidget()
        self._tree.setObjectName("taskTree")
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        root.addWidget(self._tree, 1)

        # Chevron toggle button per open project card, keyed by project name
        # (not item identity — the item/widget are rebuilt on every refresh,
        # and Python `id()` can be reused after GC, so keying by identity
        # risks updating a stale/deleted button).
        self._chevron_labels: dict[str, QToolButton] = {}

        # Collapse state survives a refresh (else every ledger write would
        # snap every open card shut again) — keyed by the item's own
        # `UserRole` id string, recording every explicit expand/collapse the
        # user makes. In-memory only; does NOT persist across app restarts
        # (design tradeoff — see the A8 done-note to Lead).
        self._manual_state: dict[str, bool] = {}
        self._tree.itemCollapsed.connect(self._on_item_collapsed)
        self._tree.itemExpanded.connect(self._on_item_expanded)

        self.refresh_all()

    # ──────────────────────────────────────────────────────────────
    def _remember(self, item: QTreeWidgetItem, expanded: bool) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self._manual_state[key] = expanded

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._remember(item, True)
        self._apply_expanded_visual(item, True)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        self._remember(item, False)
        self._apply_expanded_visual(item, False)

    def _apply_expanded_visual(self, item: QTreeWidgetItem, expanded: bool) -> None:
        """Repaint the ▸/▾ chevron for *item* to match its expanded state —
        a project card's chevron button, or the text-embedded chevron on a
        goal/feature row (task rows carry neither and are left untouched)."""
        chevron = "▾" if expanded else "▸"
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str) and key.startswith("project:"):
            btn = self._chevron_labels.get(key[len("project:") :])
            if btn is not None:
                btn.setText(chevron)
            return
        base = item.data(0, _ROLE_BASE_LABEL)
        if isinstance(base, str):
            item.setText(0, f"{chevron}  {base}")

    def _restore_expansion(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        # Default open: a card/group the user never touched starts expanded
        # so first-run isn't a wall of collapsed rows; feature rows default
        # shut (tasks are the noisiest level). A row the user explicitly
        # expanded/collapsed keeps that state across refreshes.
        default_open = isinstance(key, str) and not key.startswith("feature:")
        expanded = self._manual_state.get(key, default_open)
        item.setExpanded(expanded)
        # setExpanded() only *emits* itemExpanded/itemCollapsed (and thus
        # fixes up the chevron) when the state actually changes — apply the
        # visual explicitly too so a no-op (already-default) case is correct.
        self._apply_expanded_visual(item, expanded)
        for i in range(item.childCount()):
            self._restore_expansion(item.child(i))

    # ──────────────────────────────────────────────────────────────
    def refresh_all(self) -> None:
        """Reload every open project's ledger state and rebuild the tree."""
        for project in list_project_names():
            self.refresh_project(project)

    def refresh_project(self, project: str) -> None:
        """Reload just *project*'s ledger state — the slot `Orchestrator.
        ledgerChanged` connects to, so a single assign/done/fail/close only
        repaints the one card that actually changed."""
        state = task_ledger.load_state(project)
        existing = self._find_top_level(f"project:{project}")
        if not has_any_rows(state):
            if existing is not None:
                self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(existing))
                self._chevron_labels.pop(project, None)
            return
        if existing is not None:
            self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(existing))
        item = self._build_project_item(project, state)
        # The subtree must already be attached to the tree (addTopLevelItem)
        # before setItemWidget() calls are valid — mount after adding, then
        # restore expansion (which also depends on the chevron button
        # existing, since _apply_expanded_visual looks it up by project name).
        self._tree.addTopLevelItem(item)
        self._mount_widgets(project, item, state)
        self._restore_expansion(item)

    def _find_top_level(self, key: str) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == key:
                return it
        return None

    # ──────────────────────────────────────────────────────────────
    # tree construction (text + data only — no Qt item widgets yet)
    # ──────────────────────────────────────────────────────────────
    def _build_project_item(self, project: str, state: dict) -> QTreeWidgetItem:
        done, total = project_progress(state)
        item = QTreeWidgetItem([f"{project}  ({done}/{total})"])
        item.setData(0, Qt.ItemDataRole.UserRole, f"project:{project}")
        for group in state.get("groups", []):
            item.addChild(self._build_group_item(project, group))
        return item

    def _build_group_item(self, project: str, group: dict) -> QTreeWidgetItem:
        rows_all = [r for f in group.get("features", []) for r in f.get("rows", [])]
        done = sum(1 for r in rows_all if r.get("status") == "ok")
        goal = group.get("goal", "")
        key = f"group:{project}:{group.get('date', '')}:{goal}"
        base_label = f"\U0001f3af {goal}  ({done}/{len(rows_all)})"
        item = QTreeWidgetItem([f"▸  {base_label}"])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setData(0, _ROLE_BASE_LABEL, base_label)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(0, QColor("#e4e4e7"))
        for feat in group.get("features", []):
            item.addChild(self._build_feature_item(project, group, feat))
        return item

    def _build_feature_item(self, project: str, group: dict, feature: dict) -> QTreeWidgetItem:
        name = feature.get("name", "")
        key = f"feature:{project}:{group.get('date', '')}:{group.get('goal', '')}:{name}"
        base_label = f"{feature_emoji(feature)} {name}"
        item = QTreeWidgetItem([f"▸  {base_label}"])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setData(0, _ROLE_BASE_LABEL, base_label)
        item.setForeground(0, QColor("#a1a1aa"))
        for row in feature.get("rows", []):
            item.addChild(self._build_row_item(row))
        return item

    def _build_row_item(self, row: dict) -> QTreeWidgetItem:
        glyph, color = status_glyph(row.get("status", ""))
        summary = row.get("summary", "")
        label = f"{glyph} {row.get('role', '')} · {summary}"
        item = QTreeWidgetItem([label])
        item.setIcon(0, _row_status_icon(glyph, color))
        item.setForeground(0, QColor("#a1a1aa"))
        item.setToolTip(0, f"{row.get('cwd', '')}\n{summary}")
        return item

    # ──────────────────────────────────────────────────────────────
    # item widget (project card) — mounted only once the whole subtree is
    # attached to the tree, since setItemWidget() needs a valid model index.
    # ──────────────────────────────────────────────────────────────
    def _mount_widgets(self, project: str, item: QTreeWidgetItem, state: dict) -> None:
        self._mount_project_row(project, item, state)

    def _mount_project_row(self, project: str, item: QTreeWidgetItem, state: dict) -> None:
        """Replace the project item's column-0 rendering with a sidebar-style
        card: chevron toggle + avatar (color shared with the PROJECTS
        sidebar via `_avatar_color`) + name + progress badge + mini bar +
        ↗ open-INDEX.md button."""
        done, total = project_progress(state)
        ratio = (done / total) if total else 0.0
        color = usage_color(ratio) if total else "#52525b"

        holder = QWidget()
        holder.setObjectName("taskProjectCard")
        holder.setStyleSheet(
            "#taskProjectCard {"
            " background: #1a1a1e;"
            " border: 1px solid #27272a;"
            " border-radius: 10px;"
            "}"
        )
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        chevron = QToolButton()
        chevron.setText("▸")
        chevron.setAutoRaise(True)
        chevron.setFixedSize(16, 16)
        chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        chevron.setStyleSheet(
            "QToolButton {"
            " color: #71717a; background: transparent; border: none;"
            " font-size: 10px; font-weight: 700; }"
            "QToolButton:hover { color: #d4d4d8; }"
        )
        chevron.clicked.connect(lambda: item.setExpanded(not item.isExpanded()))
        row.addWidget(chevron)
        self._chevron_labels[project] = chevron

        avatar = QLabel(_initials(project))
        avatar.setFixedSize(24, 24)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {_avatar_color(project)}; color: #ffffff; font-size: 10px;"
            f" font-weight: 800; border-radius: 12px;"
        )
        row.addWidget(avatar)

        name = QLabel(project)
        name.setStyleSheet("color: #e4e4e7; font-size: 13px; font-weight: 700;")
        row.addWidget(name, 1)

        badge = QLabel(f"{done}/{total}")
        badge.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 700;")
        row.addWidget(badge)

        bar = QProgressBar()
        bar.setObjectName("taskMiniBar")
        bar.setMaximum(max(total, 1))
        bar.setValue(done)
        bar.setTextVisible(False)
        bar.setFixedWidth(48)
        bar.setFixedHeight(6)
        bar.setStyleSheet(
            "QProgressBar#taskMiniBar {"
            " background: #27272a; border: none; border-radius: 3px; }"
            "QProgressBar#taskMiniBar::chunk {"
            f" background: {color}; border-radius: 3px; }}"
        )
        row.addWidget(bar)

        open_btn = QToolButton()
        open_btn.setText("↗")  # ↗
        open_btn.setToolTip(f"เปิด INDEX.md เต็ม ({project})")
        open_btn.setFixedSize(20, 20)
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setStyleSheet(
            "QToolButton {"
            " color: #71717a; background: transparent; border: none; border-radius: 6px; }"
            "QToolButton:hover { background: #27272a; color: #d4d4d8; }"
        )
        open_btn.clicked.connect(lambda: self._open_index(project))
        row.addWidget(open_btn)

        self._tree.setItemWidget(item, 0, holder)

    @staticmethod
    def _open_index(project: str) -> None:
        path = task_ledger.index_path(project)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
