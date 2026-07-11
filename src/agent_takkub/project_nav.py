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
    QTimer,
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

from . import cockpit_theme, task_ledger
from .config import list_project_names
from .token_meter import usage_color

# Poll interval for the "other projects with pending tasks" section — no
# ledgerChanged signal is wired to this widget (that connection lives in
# main_window.py, out of this widget's scope), so it self-refreshes instead.
_PENDING_POLL_MS = 6_000

_SIDEBAR_QSS = f"""
#projectSidebar {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    border-right: 1px solid {cockpit_theme.BORDER_HAIRLINE};
}}
#projectSidebarHeader {{
    color: {cockpit_theme.TEXT_FAINT};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 14px 16px 8px 16px;
}}
QListWidget#projectList {{
    background: transparent;
    border: none;
    outline: 0;
    padding: 2px 8px;
}}
QListWidget#projectList::item {{
    border-radius: {cockpit_theme.RADIUS_SM}px;
    margin: 2px 0;
    padding: 0;
}}
QListWidget#projectList::item:hover {{
    background: {cockpit_theme.GROUND_PANEL};
}}
QListWidget#projectList::item:selected {{
    background: {cockpit_theme.GROUND_SELECT};
}}
#addProjectBtn {{
    background: {cockpit_theme.GOLD_CHIP_BG};
    color: {cockpit_theme.GOLD_CHIP_TEXT};
    border: 1px dashed {cockpit_theme.GOLD_CHIP_BORDER};
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 9px;
    margin: 8px 12px 12px 12px;
    font-size: 12px;
    font-weight: 600;
}}
#addProjectBtn:hover {{
    background: rgba(227,179,65,0.18);
    border-color: {cockpit_theme.ACCENT_GOLD};
    color: {cockpit_theme.ACCENT_GOLD};
}}
#sidebarToggleBtn {{
    background: transparent;
    color: {cockpit_theme.TEXT_FAINT};
    border: none;
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 7px;
    margin: 6px 12px 0 12px;
    font-size: 12px;
    font-weight: 600;
}}
#sidebarToggleBtn:hover {{
    background: {cockpit_theme.GROUND_PANEL};
    color: {cockpit_theme.TEXT_MUTED};
}}
#sidebarUsage {{
    background: {cockpit_theme.GROUND_BODY};
    border-top: 1px solid {cockpit_theme.BORDER_HAIRLINE};
}}
#pendingHeader {{
    color: {cockpit_theme.TEXT_FAINT};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 8px 16px 4px 16px;
}}
QListWidget#pendingList {{
    background: transparent;
    border: none;
    outline: 0;
    padding: 0 8px;
}}
QListWidget#pendingList::item {{
    border-radius: {cockpit_theme.RADIUS_SM}px;
    margin: 1px 0;
    padding: 5px 8px;
    color: {cockpit_theme.TEXT_MUTED};
    font-size: 12px;
}}
QListWidget#pendingList::item:hover {{
    background: {cockpit_theme.GROUND_PANEL};
    color: {cockpit_theme.TEXT_SECONDARY};
}}
"""

# Sidebar widths: full list vs. the collapsed avatar-only rail.
_EXPANDED_W = 212
_COLLAPSED_W = 64
_AVATAR_PX = 34

# Deterministic per-project avatar tint (hash of the name → fixed palette).
# Canonical values now live in cockpit_theme.AVATAR_TINTS (a distinct purpose
# from role identity); aliased here so existing `project_nav._AVATAR_COLORS`
# references keep working.
_AVATAR_COLORS = cockpit_theme.AVATAR_TINTS


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
        self._name.setStyleSheet(
            f"color: {cockpit_theme.TEXT_SECONDARY}; font-size: 13px; font-weight: 600;"
        )

        self._badge = QLabel("")
        self._badge.setStyleSheet(f"color: {cockpit_theme.TEXT_FAINT}; font-size: 11px;")

        self._lay.addWidget(self._avatar)
        self._lay.addWidget(self._name, 1)
        self._lay.addWidget(self._badge)

        self._paint_avatar()
        self.set_collapsed(collapsed)

    # -- painting -----------------------------------------------------
    def _paint_avatar(self) -> None:
        # Selection ring = gold (the design system's one selection accent),
        # replacing the old white ring.
        ring = cockpit_theme.ACCENT_GOLD if self._selected else "rgba(0,0,0,0)"
        self._avatar.setStyleSheet(
            f"background: {_avatar_color(self._name_text)};"
            f" color: {cockpit_theme.TEXT_PRIMARY}; font-size: 12px; font-weight: 800;"
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
        color = cockpit_theme.TEXT_PRIMARY if selected else cockpit_theme.TEXT_SECONDARY
        self._name.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 600;")
        self._paint_avatar()

    def set_usage(self, ratio: float | None) -> None:
        if ratio is None:
            self._badge.setText("")
            self._badge.setToolTip("")
            return
        pct = int(ratio * 100)
        # 🧠 prefix + tooltip: the bare "33%" had no legend (walkthrough
        # cluster D item 2) — nothing said whether it was token budget, disk,
        # or something else. It's the busiest pane's context-window fill
        # (status_header._update_status: peak prompt/limit ratio across the
        # project's panes), so label it as that, in-place, no extra dialog.
        self._badge.setText(f"\U0001f9e0 {pct}%")
        self._badge.setStyleSheet(f"color: {usage_color(ratio)}; font-size: 11px;")
        self._badge.setToolTip(f"Context window usage {pct}% — pane ที่ใช้เยอะสุดในโปรเจคนี้ตอนนี้")

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
    # Emitted when the user clicks a row in the "โปรเจคอื่นที่มี task ค้าง"
    # section (walkthrough cluster D item 1) — the sidebar only knows the
    # project *name* to open, not how to build/register its tab (that's
    # main_window._open_project_tab's job), so it hands the request up.
    openProjectRequested = pyqtSignal(str)

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

        # "โปรเจคอื่นที่มี task ค้าง" — sidebar only lists projects with an
        # *open tab*, but the task dock (right side) shows every project with
        # a ledger row, open tab or not. That mismatch read as "sidebar and
        # dock disagree about which projects exist" (walkthrough cluster D
        # item 1). This section closes the gap: any project with an open
        # (`working`) ledger row that ISN'T already an open tab gets listed
        # here — click to open it. Hidden entirely when there's nothing to
        # show, so it costs no space on the common case.
        self._pending_header = QLabel("โปรเจคอื่นที่มี task ค้าง")
        self._pending_header.setObjectName("pendingHeader")
        self._pending_header.hide()
        sb.addWidget(self._pending_header)

        self._pending_list = QListWidget(sidebar)
        self._pending_list.setObjectName("pendingList")
        self._pending_list.setFrameShape(QListWidget.Shape.NoFrame)
        self._pending_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pending_list.setMaximumHeight(96)
        self._pending_list.itemClicked.connect(self._on_pending_item_clicked)
        self._pending_list.hide()
        sb.addWidget(self._pending_list)

        # No ledgerChanged hookup here (that connection lives in
        # main_window.py, out of this widget's scope) — poll instead so a
        # fresh assign in another tab still surfaces here within a few
        # seconds without any wiring on the caller's side.
        self._pending_timer = QTimer(self)
        self._pending_timer.setInterval(_PENDING_POLL_MS)
        self._pending_timer.timeout.connect(self.refresh_pending_projects)
        self._pending_timer.start()

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

        # ── right content ───────────────────────────────────────────
        self._stack = QStackedWidget(self)

        root.addWidget(sidebar)
        root.addWidget(self._stack, 1)

        self.refresh_pending_projects()

    # ------------------------------------------------------------------
    # "other projects with pending tasks" section
    # ------------------------------------------------------------------
    def refresh_pending_projects(self) -> None:
        """Rebuild the pending-projects section from the task ledger.

        A project qualifies when it has at least one `working` ledger row
        (an assign that never called `takkub done`) and isn't already an
        open tab. Cheap: `task_ledger.load_state` is a small JSON read per
        project, and there are only ever a handful of configured projects.
        """
        open_names = {self._row_widget(i)._name_text for i in range(self._list.count())}
        try:
            all_names = list_project_names()
        except Exception:
            all_names = []

        self._pending_list.clear()
        for name in all_names:
            if name in open_names:
                continue
            count = self._pending_task_count(name)
            if count <= 0:
                continue
            item = QListWidgetItem(f"○  {name}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(f"เปิด '{name}' ({count} task ค้าง)")
            self._pending_list.addItem(item)

        has_pending = self._pending_list.count() > 0
        self._pending_header.setVisible(has_pending and not self._collapsed)
        self._pending_list.setVisible(has_pending and not self._collapsed)

    @staticmethod
    def _pending_task_count(project: str) -> int:
        try:
            state = task_ledger.load_state(project)
        except Exception:
            return 0
        count = 0
        for group in state.get("groups", []):
            for feat in group.get("features", []):
                for row in feat.get("rows", []):
                    if row.get("status") == "working":
                        count += 1
        return count

    def _on_pending_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(name, str) and name:
            self.openProjectRequested.emit(name)

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
        # Pending section only makes sense expanded (it's read as list rows,
        # not avatars) — re-run refresh_pending_projects() so it re-applies
        # its own has_pending && !collapsed visibility check.
        self.refresh_pending_projects()
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
        self.refresh_pending_projects()  # opened project drops out of "pending"
        return index

    def insertTab(self, index: int, w: QWidget, label: str) -> int:
        self._stack.insertWidget(index, w)
        self._insert_list_row(index, label)
        self.refresh_pending_projects()
        return index

    def removeTab(self, index: int) -> None:
        w = self._stack.widget(index)
        if w is not None:
            self._stack.removeWidget(w)
        item = self._list.takeItem(index)
        if item is not None:
            del item
        self.refresh_pending_projects()  # closed project may reappear as pending

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

    # ------------------------------------------------------------------
    def _insert_list_row(self, index: int, label: str) -> None:
        item = QListWidgetItem()
        row = _ProjectRow(label, collapsed=self._collapsed)
        # Fixed-height hint keeps rows aligned in both modes; the list clamps
        # width to the (animating) sidebar, so only height matters here.
        item.setSizeHint(QSize(0, _AVATAR_PX + 18))
        self._list.insertItem(index, item)
        self._list.setItemWidget(item, row)
