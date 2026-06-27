"""ProjectNav: a left sidebar project list + right stacked content area.

Drop-in replacement for the old top `QTabWidget` that hosted one ProjectTab
per project. The redesign (2026-06-26) moves project selection from a top tab
strip to a **left sidebar list**; the selected project's panes fill the right.
This is presentation-only — it exposes the slice of the `QTabWidget` API that
MainWindow already used (`addTab`/`insertTab`/`removeTab`/`widget`/`count`/
`currentWidget`/`currentIndex`/`setCurrentIndex`/`indexOf` + a `currentChanged`
signal) so the ~dozen call sites keep working unchanged. Tab-bar-only concerns
(the "+" pseudo-tab, per-tab close button, context menu) become sidebar idioms
surfaced as dedicated signals: `addRequested`, `closeRequested(index)`,
`contextMenuRequested(index, globalPos)`.

Row index == stacked-widget index == list row, kept in lockstep at all times.
"""

from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .token_meter import usage_color

_SIDEBAR_QSS = """
#projectSidebar {
    background: #0e0e10;
    border-right: 1px solid #27272a;
}
#projectSidebarHeader {
    color: #52525b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 14px 16px 8px 16px;
}
QListWidget#projectList {
    background: transparent;
    border: none;
    outline: 0;
    padding: 2px 8px;
}
QListWidget#projectList::item {
    border-radius: 8px;
    margin: 2px 0;
    padding: 0;
}
QListWidget#projectList::item:hover {
    background: #18181b;
}
QListWidget#projectList::item:selected {
    background: #1e1b2e;
}
#addProjectBtn {
    background: transparent;
    color: #818cf8;
    border: 1px dashed #3f3f46;
    border-radius: 8px;
    padding: 9px;
    margin: 8px 12px 12px 12px;
    font-size: 12px;
    font-weight: 600;
}
#addProjectBtn:hover {
    background: #18181b;
    border-color: #6366f1;
    color: #a5b4fc;
}
#sidebarToggleBtn {
    background: transparent;
    color: #52525b;
    border: none;
    border-radius: 8px;
    padding: 7px;
    margin: 6px 12px 0 12px;
    font-size: 12px;
    font-weight: 600;
}
#sidebarToggleBtn:hover {
    background: #18181b;
    color: #a1a1aa;
}
#sidebarUsage {
    background: #0b0b0d;
    border-top: 1px solid #1c1c1f;
}
"""

# Sidebar widths: full list vs. the collapsed avatar-only rail.
_EXPANDED_W = 212
_COLLAPSED_W = 64
_AVATAR_PX = 34

# Deterministic per-project avatar tint (hash of the name → fixed palette).
_AVATAR_COLORS = (
    "#6366f1",
    "#8b5cf6",
    "#ec4899",
    "#f43f5e",
    "#f59e0b",
    "#10b981",
    "#06b6d4",
    "#3b82f6",
    "#a855f7",
    "#14b8a6",
)


def _avatar_color(name: str) -> str:
    return _AVATAR_COLORS[sum(ord(c) for c in name) % len(_AVATAR_COLORS)]


def _initials(name: str) -> str:
    """First 2 non-space characters, upper-cased (e.g. 'agent takkub' → 'AG')."""
    letters = [c for c in name if not c.isspace()]
    return "".join(letters[:2]).upper() or "?"


class _ProjectRow(QWidget):
    """Custom sidebar row: a round initials avatar + project name + usage badge.

    Background stays transparent so the QListWidget's :selected / :hover
    highlight shows through behind it. In *collapsed* mode the name and badge
    hide and only the centered avatar remains — the narrow-rail look.
    """

    def __init__(self, name: str, collapsed: bool = False) -> None:
        super().__init__()
        self._name_text = name
        self._selected = False

        self._lay = QHBoxLayout(self)
        self._lay.setSpacing(9)

        self._avatar = QLabel(_initials(name))
        self._avatar.setFixedSize(_AVATAR_PX, _AVATAR_PX)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._name = QLabel(name)
        self._name.setStyleSheet("color: #d4d4d8; font-size: 13px; font-weight: 600;")

        self._badge = QLabel("")
        self._badge.setStyleSheet(
            "color: #52525b; font-size: 11px; font-variant-numeric: tabular-nums;"
        )

        self._lay.addWidget(self._avatar)
        self._lay.addWidget(self._name, 1)
        self._lay.addWidget(self._badge)

        self._paint_avatar()
        self.set_collapsed(collapsed)

    # -- painting -----------------------------------------------------
    def _paint_avatar(self) -> None:
        ring = "#ffffff" if self._selected else "rgba(0,0,0,0)"
        self._avatar.setStyleSheet(
            f"background: {_avatar_color(self._name_text)};"
            f" color: #ffffff; font-size: 12px; font-weight: 800;"
            f" border-radius: {_AVATAR_PX // 2}px; border: 2px solid {ring};"
        )

    # -- state --------------------------------------------------------
    def set_name(self, name: str) -> None:
        self._name_text = name
        self._name.setText(name)
        self._avatar.setText(_initials(name))
        self.setToolTip(name)
        self._paint_avatar()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        color = "#ffffff" if selected else "#d4d4d8"
        self._name.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 600;")
        self._paint_avatar()

    def set_usage(self, ratio: float | None) -> None:
        if ratio is None:
            self._badge.setText("")
            return
        self._badge.setText(f"{int(ratio * 100)}%")
        self._badge.setStyleSheet(
            f"color: {usage_color(ratio)}; font-size: 11px; font-variant-numeric: tabular-nums;"
        )

    def set_collapsed(self, collapsed: bool) -> None:
        """Hide/show the name + badge; re-center the avatar in the rail."""
        self._name.setVisible(not collapsed)
        self._badge.setVisible(not collapsed)
        # Collapsed: center the avatar in the rail. The row is only ~48px wide
        # (rail 64 − list padding 16), so a 34px avatar needs (48−34)/2 ≈ 7px
        # side margins; the old 15px overflowed and shoved the avatar left.
        if collapsed:
            self._lay.setContentsMargins(7, 8, 7, 8)
            self.setToolTip(self._name_text)
        else:
            self._lay.setContentsMargins(12, 8, 12, 8)
            self.setToolTip("")


class ProjectNav(QWidget):
    """Sidebar project list + stacked content. QTabWidget-compatible subset."""

    currentChanged = pyqtSignal(int)
    addRequested = pyqtSignal()
    closeRequested = pyqtSignal(int)
    contextMenuRequested = pyqtSignal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── left sidebar ────────────────────────────────────────────
        sidebar = QWidget(self)
        sidebar.setObjectName("projectSidebar")
        sidebar.setFixedWidth(_EXPANDED_W)
        sidebar.setStyleSheet(_SIDEBAR_QSS)
        # Held so the header's ☰ toggle can slide it between the full list and
        # the narrow avatar rail. _collapsed tracks the rail state; _anim keeps
        # the running width animation alive (else PyQt GCs it mid-flight).
        self._sidebar = sidebar
        self._collapsed = False
        self._anim: QParallelAnimationGroup | None = None
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)

        self._header = QLabel("PROJECTS")
        self._header.setObjectName("projectSidebarHeader")
        sb.addWidget(self._header)

        self._list = QListWidget(sidebar)
        self._list.setObjectName("projectList")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        sb.addWidget(self._list, 1)

        # Sidebar collapse/expand toggle. Lives in the sidebar footer (right
        # above "New project") so the control that hides the sidebar sits inside
        # the thing it acts on, instead of off in the bottom status bar. The
        # glyph flips with state: «  Collapse (expanded) ↔ » (rail).
        self._toggle_btn = QPushButton("«  Collapse", sidebar)
        self._toggle_btn.setObjectName("sidebarToggleBtn")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setToolTip("Collapse the projects sidebar to avatars / expand")
        self._toggle_btn.clicked.connect(lambda: self.toggle_sidebar())
        sb.addWidget(self._toggle_btn)

        self._add_btn = QPushButton("+   New project", sidebar)
        self._add_btn.setObjectName("addProjectBtn")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(self.addRequested.emit)
        sb.addWidget(self._add_btn)

        # ── usage/limit meter footer ────────────────────────────────
        # The meter QLabel itself is owned by MainWindow (the limit_panel mixin
        # keeps its text/colour in sync); the sidebar only reserves a stable,
        # always-visible slot for it at the bottom edge. Parking it here — not
        # in the bottom status bar — is what keeps it from being clipped off the
        # right edge when the status bar's button row overflows on small/narrow
        # displays. Hidden together with the rest of the chrome in the rail.
        self._usage_footer = QWidget(sidebar)
        self._usage_footer.setObjectName("sidebarUsage")
        self._usage_widget: QWidget | None = None
        self._usage_lay = QVBoxLayout(self._usage_footer)
        self._usage_lay.setContentsMargins(12, 8, 12, 10)
        self._usage_lay.setSpacing(0)
        sb.addWidget(self._usage_footer)

        # ── right content ───────────────────────────────────────────
        self._stack = QStackedWidget(self)

        root.addWidget(sidebar)
        root.addWidget(self._stack, 1)

    # ------------------------------------------------------------------
    # sidebar collapse/expand (the ☰ "slide menu" toggle)
    # ------------------------------------------------------------------
    def toggle_sidebar(self) -> bool:
        """Slide between the full list and the avatar rail. True if collapsed."""
        self.set_sidebar_collapsed(not self._collapsed)
        return self._collapsed

    def set_sidebar_collapsed(self, collapsed: bool, *, animate: bool = True) -> None:
        """Collapse to the narrow avatar-only rail, or expand to the full list."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed

        # Swap each row's presentation (name/badge hidden → centered avatar).
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is not None:
                rw.set_collapsed(collapsed)
        self._header.setVisible(not collapsed)
        self._add_btn.setText("+" if collapsed else "+   New project")
        self._toggle_btn.setText("»" if collapsed else "«  Collapse")
        # The usage meter has no sensible rail form (its text needs ~150px), so
        # it simply hides while collapsed; per-project usage is still on the
        # avatar tooltip. Re-shows on expand.
        self._usage_footer.setVisible(not collapsed)

        target = _COLLAPSED_W if collapsed else _EXPANDED_W
        if not animate:
            self._sidebar.setFixedWidth(target)
            return
        self._animate_width(target)

    def is_sidebar_collapsed(self) -> bool:
        return self._collapsed

    def _animate_width(self, target: int) -> None:
        """Tween the sidebar's min+max width together for a smooth slide."""
        start = self._sidebar.width()
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(self._sidebar, prop, self)
            anim.setDuration(190)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(anim)
        self._anim = group  # keep a reference so PyQt doesn't GC it mid-run
        group.start()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _row_widget(self, index: int) -> _ProjectRow | None:
        item = self._list.item(index)
        if item is None:
            return None
        w = self._list.itemWidget(item)
        return w if isinstance(w, _ProjectRow) else None

    def _on_row_changed(self, row: int) -> None:
        # Keep the stack in lockstep and repaint selection accents.
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is not None:
                rw.set_selected(i == row)
        self.currentChanged.emit(row)

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        index = self._list.row(item)
        self.contextMenuRequested.emit(index, self._list.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # QTabWidget-compatible API
    # ------------------------------------------------------------------
    def count(self) -> int:
        return self._stack.count()

    def widget(self, index: int) -> QWidget | None:
        return self._stack.widget(index)

    def currentWidget(self) -> QWidget | None:
        return self._stack.currentWidget()

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def indexOf(self, w: QWidget) -> int:
        return self._stack.indexOf(w)

    def setCurrentIndex(self, index: int) -> None:
        # Drive selection through the list so the accent + currentChanged path
        # runs exactly once. _on_row_changed mirrors it onto the stack.
        if 0 <= index < self._list.count():
            self._list.setCurrentRow(index)

    def addTab(self, w: QWidget, label: str) -> int:
        index = self._stack.addWidget(w)
        self._insert_list_row(index, label)
        return index

    def insertTab(self, index: int, w: QWidget, label: str) -> int:
        self._stack.insertWidget(index, w)
        self._insert_list_row(index, label)
        return index

    def removeTab(self, index: int) -> None:
        w = self._stack.widget(index)
        if w is not None:
            self._stack.removeWidget(w)
        item = self._list.takeItem(index)
        if item is not None:
            del item

    def setTabText(self, index: int, text: str) -> None:
        rw = self._row_widget(index)
        if rw is not None:
            rw.set_name(text)

    def setTabToolTip(self, index: int, text: str) -> None:
        item = self._list.item(index)
        if item is not None:
            item.setToolTip(text)

    def set_usage(self, index: int, ratio: float | None) -> None:
        """Sidebar-specific: show a project's peak context-usage %. (Replaces
        the old per-tab `setTabText(... · NN%)` badge.)"""
        rw = self._row_widget(index)
        if rw is not None:
            rw.set_usage(ratio)

    def mount_usage_widget(self, widget: QWidget) -> None:
        """Adopt an externally-owned usage/limit meter into the sidebar footer.

        MainWindow owns the meter QLabel (the limit_panel mixin refreshes its
        text/style); the sidebar just gives it a stable home so it can never be
        clipped off the bottom status bar on small screens. Centered, and
        hidden while the sidebar is collapsed to the avatar rail.
        """
        self._usage_widget = widget
        self._usage_lay.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
        widget.setVisible(not self._collapsed)

    # ------------------------------------------------------------------
    def _insert_list_row(self, index: int, label: str) -> None:
        item = QListWidgetItem()
        row = _ProjectRow(label, collapsed=self._collapsed)
        # Fixed-height hint keeps rows aligned in both modes; the list clamps
        # width to the (animating) sidebar, so only height matters here.
        item.setSizeHint(QSize(0, _AVATAR_PX + 18))
        self._list.insertItem(index, item)
        self._list.setItemWidget(item, row)
