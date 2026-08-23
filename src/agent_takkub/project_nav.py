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

Each `_ProjectRow` also embeds that project's `ProjectExplorer` (file tree)
directly beneath its avatar/name/badge strip (2026-08-23, replacing the
short-lived #365-phase-1 QSplitter panel between the sidebar and the pane
area — user feedback: three columns read wrong, the tree belongs to its
project's own card, VS Code/Obsidian-style). Only the selected row ever shows
its tree; a chevron on the row itself (visible only while selected) lets the
user manually collapse/expand it, persisted per-project in QSettings. The
`ProjectExplorer` instance itself is still owned and signal-wired by
`ProjectTab` (`project_tab.py`) — this module only reparents it into the
row's slot for display and hands it back on `removeTab` so `ProjectTab`'s own
teardown still owns its lifecycle.
"""

from __future__ import annotations

from typing import ClassVar

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSettings,
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
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, task_ledger
from .config import list_project_names
from .path_safe import safe_segment
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
#tunnelIndicator {{
    border-radius: 5px;
    background: {cockpit_theme.TEXT_FAINT};
    border: 1px solid rgba(0,0,0,0.25);
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
QPushButton#projectRowChevron {{
    background: transparent;
    color: {cockpit_theme.TEXT_FAINT};
    border: none;
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 0;
    font-size: 11px;
    font-weight: 700;
}}
QPushButton#projectRowChevron:hover {{
    background: {cockpit_theme.GROUND_PANEL};
    color: {cockpit_theme.TEXT_SECONDARY};
}}
QPushButton#projectRowChevron[expanded="true"] {{
    color: {cockpit_theme.ACCENT_GOLD};
}}
#projectExplorerSlot {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    border-top: 1px solid {cockpit_theme.BORDER_HAIRLINE};
}}
"""

# Sidebar widths: full list vs. the collapsed avatar-only rail. Expanded is
# resizable (via the QSplitter between sidebar and pane stack) up to
# _SIDEBAR_MAX_W — a project's embedded file tree needs more room than the
# original fixed-width sidebar gave it (#365 feedback 2026-08-23: Explorer
# moved under the project card, see project_nav module docstring below).
_EXPANDED_W = 212
_COLLAPSED_W = 64
_SIDEBAR_MAX_W = 16_777_215  # QWIDGETSIZE_MAX — same "unbounded once expanded" convention _tasks_dock_collapse_toggled uses in main_window.py
_AVATAR_PX = 34

# Height floor for a project row's embedded file tree. The tree is sized to
# *fill* the sidebar list down to its bottom edge (ProjectNav._fit_explorer
# re-derives it from the list viewport minus every row's header each time the
# sidebar resizes / selection changes / a row is added), so the only constant
# left is the minimum it may shrink to before the list itself starts
# scrolling — user feedback 2026-08-23: a fixed 260px left a dead band under
# the tree on any tall window.
_EXPLORER_MIN_H = 120

# Per-project "explorer expanded" flag persists under the same QSettings
# domain ProjectTab used before the explorer moved into the sidebar (2026-08-23)
# — same org/app pair MainWindow uses for window geometry, keyed
# `explorer/<safe_segment(project_name)>/collapsed`.
_EXPLORER_SETTINGS_ORG = "agent-takkub"
_EXPLORER_SETTINGS_APP = "cockpit"

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
    """Custom sidebar row: a round initials avatar + project name + usage badge,
    with that project's `ProjectExplorer` (file tree) embedded directly below —
    VS Code/Obsidian-style "card expands under itself" rather than a separate
    splitter panel (#365 feedback 2026-08-23, see project_nav module docstring).

    Background stays transparent so the QListWidget's :selected / :hover
    highlight shows through behind the header strip. In *collapsed* (rail)
    mode the name/badge/chevron/tree all hide and only the centered avatar
    remains. The tree is only ever shown for the currently **selected**
    row — switching projects auto-hides the previous row's tree — and only
    when that row's own `_expanded` flag (persisted per-project) is set; a
    chevron on the row itself is the one way to flip that flag manually.
    """

    # Emitted whenever the user clicks this row's chevron — carries the new
    # expanded state so ProjectNav can persist it (QSettings, keyed by the
    # project's stable identity, not the possibly-renamed display label).
    explorerToggled = pyqtSignal(bool)

    def __init__(
        self,
        name: str,
        collapsed: bool = False,
        explorer: QWidget | None = None,
        expanded: bool = True,
        project_key: str | None = None,
    ) -> None:
        super().__init__()
        self._name_text = name
        self._project_key = project_key or name
        self._selected = False
        self._rail_collapsed = False
        self._explorer = explorer
        self._expanded = expanded

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QWidget(self)
        self._lay = QHBoxLayout(top)
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

        self._chevron = QPushButton("▾" if expanded else "▸", top)
        self._chevron.setObjectName("projectRowChevron")
        self._chevron.setFixedSize(18, 18)
        self._chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chevron.setToolTip("ยุบ/ขยาย file tree")
        self._chevron.clicked.connect(self._on_chevron_clicked)

        self._lay.addWidget(self._avatar)
        self._lay.addWidget(self._name, 1)
        self._lay.addWidget(self._badge)
        self._lay.addWidget(self._chevron)
        outer.addWidget(top)

        # The explorer container is a plain slot — the actual ProjectExplorer
        # widget is reparented in here by ProjectNav (it's owned/constructed
        # by ProjectTab, which keeps the signal wiring; this row only hosts
        # its visual presentation). Fixed height: the tree scrolls internally
        # instead of growing the sidebar item without bound.
        self._explorer_container = QWidget(self)
        self._explorer_container.setObjectName("projectExplorerSlot")
        ec_lay = QVBoxLayout(self._explorer_container)
        ec_lay.setContentsMargins(0, 0, 0, 0)
        ec_lay.setSpacing(0)
        if explorer is not None:
            explorer.setParent(self._explorer_container)
            explorer.setFixedHeight(_EXPLORER_MIN_H)
            ec_lay.addWidget(explorer)
        outer.addWidget(self._explorer_container)
        self._top = top

        self._paint_avatar()
        self.set_collapsed(collapsed)  # also runs the first _update_explorer_visibility()

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
        # Only the selected (active) project's tree is ever on screen — the
        # VS Code/Obsidian "one card expanded at a time" behavior. Losing
        # selection force-hides the tree but never touches `_expanded`
        # (the user's remembered preference for when this project is picked
        # again).
        self._update_explorer_visibility()

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
        self._rail_collapsed = collapsed
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
        self._update_explorer_visibility()

    def header_height(self) -> int:
        """Height of just the card row (avatar/name/badge/chevron) — what the
        row occupies when its tree is hidden. ProjectNav sums these across
        every row to find how much of the list viewport is left for the one
        visible tree."""
        return self._top.sizeHint().height()

    def explorer_height(self) -> int:
        return self._explorer.height() if self._explorer is not None else 0

    def set_explorer_height(self, h: int) -> None:
        """Pin the embedded tree to `h` px (floored at _EXPLORER_MIN_H). The
        row's sizeHint follows, so the caller must re-sync the list item."""
        if self._explorer is None:
            return
        h = max(_EXPLORER_MIN_H, int(h))
        if self._explorer.minimumHeight() != h or self._explorer.maximumHeight() != h:
            self._explorer.setFixedHeight(h)
            # Force the nested layouts to recompute now — QLayout defers
            # invalidation to the next event-loop turn, and the caller reads
            # self.sizeHint() immediately to refresh the QListWidgetItem.
            self._explorer_container.layout().activate()
            self.layout().activate()

    # -- explorer expand/collapse (chevron on the card itself) --------
    def has_explorer(self) -> bool:
        return self._explorer is not None

    def _on_chevron_clicked(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._chevron.setText("▾" if expanded else "▸")
        self._chevron.setProperty("expanded", expanded)
        self._chevron.style().unpolish(self._chevron)
        self._chevron.style().polish(self._chevron)
        self._update_explorer_visibility()
        self.explorerToggled.emit(expanded)

    def _update_explorer_visibility(self) -> None:
        """Single source of truth for chevron + tree visibility: the chevron
        only appears on the selected, non-rail-collapsed row that actually
        has an explorer; the tree under it only shows once that row is also
        manually expanded."""
        show_chevron = self._explorer is not None and self._selected and not self._rail_collapsed
        self._chevron.setVisible(show_chevron)
        show_tree = show_chevron and self._expanded
        self._explorer_container.setVisible(show_tree)
        if self._explorer is not None:
            self._explorer.setVisible(show_tree)


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

        # Per-project "explorer expanded" persistence — same org/app pair
        # MainWindow already uses for window geometry.
        self._settings = QSettings(_EXPLORER_SETTINGS_ORG, _EXPLORER_SETTINGS_APP)

        # ── left sidebar ────────────────────────────────────────────
        sidebar = QWidget(self)
        sidebar.setObjectName("projectSidebar")
        # Minimum, not fixed: the sidebar now hosts each project's embedded
        # file tree, so it needs to be drag-resizable (via the QSplitter
        # below) up to _SIDEBAR_MAX_W instead of pinned at _EXPANDED_W.
        sidebar.setMinimumWidth(_EXPANDED_W)
        sidebar.setMaximumWidth(_SIDEBAR_MAX_W)
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

        # Header row: "PROJECTS" label + a small tunnel-status dot at the
        # sidebar's top edge — the true top-left corner of the main window
        # (the sidebar sits flush against the window frame). Deliberately
        # separate from the 🌐 Remote chip in the bottom status bar: that
        # chip answers "is remote control on", this dot answers "is the
        # tunnel itself actually up right now" (a tunnel can die silently
        # after remote control started fine — see remote/tunnel.py).
        header_row = QWidget(sidebar)
        hr = QHBoxLayout(header_row)
        hr.setContentsMargins(0, 0, 12, 0)
        hr.setSpacing(6)
        self._header = QLabel("PROJECTS")
        self._header.setObjectName("projectSidebarHeader")
        hr.addWidget(self._header)
        hr.addStretch(1)
        self._tunnel_indicator = QLabel(header_row)
        self._tunnel_indicator.setObjectName("tunnelIndicator")
        self._tunnel_indicator.setFixedSize(10, 10)
        self._tunnel_indicator.setToolTip("Tunnel: not enabled")
        hr.addWidget(self._tunnel_indicator)
        sb.addWidget(header_row)

        self._list = QListWidget(sidebar)
        self._list.setObjectName("projectList")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        # The visible tree fills the list down to its bottom edge — re-fit it
        # whenever the viewport changes size (window resize, pending section
        # appearing, sidebar splitter drag).
        self._fit_pending = False
        self._list.viewport().installEventFilter(self)
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

        # QSplitter (not a plain addWidget pair) so the sidebar is
        # drag-resizable — needed now that a project's file tree lives inside
        # it and a fixed 212px is too narrow to browse comfortably. Handle
        # styling comes from MainWindow's app-wide `QSplitter::handle` QSS.
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(sidebar)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

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
    # top-left tunnel-status dot
    # ------------------------------------------------------------------
    _TUNNEL_STATE_COLORS: ClassVar[dict[str, str]] = {
        "running": cockpit_theme.STATE_OK,
        "error": cockpit_theme.STATE_ERROR,
    }

    def set_tunnel_status(self, state: str, tooltip: str) -> None:
        """Paint the sidebar-header tunnel dot. `state` is one of
        `"off"` (neutral, not running) / `"running"` (green) / `"error"`
        (red) — any other value falls back to the neutral "off" color
        rather than raising, since this is a passive status readout, not
        input validation. Pure presentation: the caller (MainWindow) is
        responsible for deciding the state from `RemoteControl`."""
        color = self._TUNNEL_STATE_COLORS.get(state, cockpit_theme.TEXT_FAINT)
        self._tunnel_indicator.setStyleSheet(
            f"#tunnelIndicator {{ border-radius:5px; background:{color}; "
            "border:1px solid rgba(0,0,0,0.25); }"
        )
        self._tunnel_indicator.setToolTip(tooltip)

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

        # Swap each row's presentation (name/badge hidden → centered avatar;
        # this also force-hides any embedded explorer tree — no room in the
        # rail — via _ProjectRow._update_explorer_visibility).
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is not None:
                rw.set_collapsed(collapsed)
                self._sync_row_size(i)
        self._header.setVisible(not collapsed)
        self._add_btn.setText("+" if collapsed else "+   New project")
        self._toggle_btn.setText("»" if collapsed else "«  Collapse")
        # Pending section only makes sense expanded (it's read as list rows,
        # not avatars) — re-run refresh_pending_projects() so it re-applies
        # its own has_pending && !collapsed visibility check.
        self.refresh_pending_projects()
        self._schedule_fit_explorer()
        target = _COLLAPSED_W if collapsed else _EXPANDED_W
        if not animate:
            self._sidebar.setFixedWidth(target)
            if not collapsed:
                self._sidebar.setMaximumWidth(_SIDEBAR_MAX_W)
            return
        self._animate_width(target)

    def is_sidebar_collapsed(self) -> bool:
        return self._collapsed

    def _animate_width(self, target: int) -> None:
        """Tween the sidebar's min+max width together for a smooth slide.

        The rail-collapsed state must stay exactly pinned at `_COLLAPSED_W`
        (min==max), but the expanded state should end up drag-resizable — so
        once an *expand* animation finishes, release `maximumWidth` back up
        to `_SIDEBAR_MAX_W` instead of leaving it pinned at `_EXPANDED_W`.
        """
        start = self._sidebar.width()
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(self._sidebar, prop, self)
            anim.setDuration(190)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(anim)
        if not self._collapsed:
            group.finished.connect(lambda: self._sidebar.setMaximumWidth(_SIDEBAR_MAX_W))
        self._anim = group  # keep a reference so PyQt doesn't GC it mid-run
        group.start()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # Qt override
        if obj is self._list.viewport() and event.type() == QEvent.Type.Resize:
            self._schedule_fit_explorer()
        return super().eventFilter(obj, event)

    def _schedule_fit_explorer(self) -> None:
        """Coalesce fit requests onto the next event-loop turn: resize events
        arrive in bursts (splitter drag, animation ticks) and the fit itself
        changes a row's sizeHint, which can re-enter via another viewport
        resize when the scrollbar appears/disappears."""
        if self._fit_pending:
            return
        self._fit_pending = True
        QTimer.singleShot(0, self._fit_explorer)

    def _fit_explorer(self) -> None:
        """Size the one visible tree so the selected row ends flush with the
        bottom of the list viewport: viewport height minus every row's header
        (and the list's inter-item spacing), floored at _EXPLORER_MIN_H — below
        the floor the list scrolls instead of squashing the tree further."""
        self._fit_pending = False
        target_index = -1
        headers = 0
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is None:
                continue
            headers += rw.header_height()
            if rw._explorer_container.isVisibleTo(rw):
                target_index = i
        if target_index < 0:
            return
        rw = self._row_widget(target_index)
        if rw is None:
            return
        spacing = self._list.spacing() * 2 * max(self._list.count(), 1)
        avail = self._list.viewport().height() - headers - spacing
        rw.set_explorer_height(avail)
        self._sync_row_size(target_index)

    def _row_widget(self, index: int) -> _ProjectRow | None:
        item = self._list.item(index)
        if item is None:
            return None
        w = self._list.itemWidget(item)
        return w if isinstance(w, _ProjectRow) else None

    def _sync_row_size(self, index: int) -> None:
        """Re-derive the QListWidgetItem's sizeHint from its row widget —
        needed whenever a row's embedded explorer container is shown/hidden,
        since QListWidget never polls itemWidget().sizeHint() on its own."""
        item = self._list.item(index)
        rw = self._row_widget(index)
        if item is not None and rw is not None:
            item.setSizeHint(rw.sizeHint())

    def _on_row_changed(self, row: int) -> None:
        # Keep the stack in lockstep and repaint selection accents. Only the
        # newly-selected row shows its explorer tree (_ProjectRow.set_selected
        # handles that) — resync every row's item height to match.
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
        for i in range(self._list.count()):
            rw = self._row_widget(i)
            if rw is not None:
                rw.set_selected(i == row)
                self._sync_row_size(i)
        self._schedule_fit_explorer()
        self.currentChanged.emit(row)

    def _load_explorer_expanded(self, project_key: str) -> bool:
        try:
            collapsed = self._settings.value(
                f"explorer/{safe_segment(project_key)}/collapsed", False, type=bool
            )
        except Exception:
            collapsed = False
        return not bool(collapsed)

    def _on_row_explorer_toggled(self, row: _ProjectRow, expanded: bool) -> None:
        self._settings.setValue(
            f"explorer/{safe_segment(row._project_key)}/collapsed", not expanded
        )
        for i in range(self._list.count()):
            if self._row_widget(i) is row:
                self._sync_row_size(i)
                break
        self._schedule_fit_explorer()

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
        self._insert_list_row(
            index,
            label,
            explorer=getattr(w, "explorer", None),
            project_key=getattr(w, "project_name", None),
        )
        self.refresh_pending_projects()  # opened project drops out of "pending"
        self._schedule_fit_explorer()
        return index

    def insertTab(self, index: int, w: QWidget, label: str) -> int:
        self._stack.insertWidget(index, w)
        self._insert_list_row(
            index,
            label,
            explorer=getattr(w, "explorer", None),
            project_key=getattr(w, "project_name", None),
        )
        self.refresh_pending_projects()
        self._schedule_fit_explorer()
        return index

    def removeTab(self, index: int) -> None:
        w = self._stack.widget(index)
        # The row's embedded explorer widget is owned (constructed, and torn
        # down) by the ProjectTab, not by this sidebar — hand it back before
        # the row is deleted so the row's destruction doesn't take the
        # explorer's QObject with it (ProjectTab.deleteLater() cascades onto
        # it correctly once it's a child of ProjectTab again).
        explorer = getattr(w, "explorer", None)
        if explorer is not None:
            explorer.setParent(w)
            explorer.hide()
        if w is not None:
            self._stack.removeWidget(w)
        item = self._list.takeItem(index)
        if item is not None:
            del item
        self.refresh_pending_projects()  # closed project may reappear as pending
        self._schedule_fit_explorer()

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
    def _insert_list_row(
        self,
        index: int,
        label: str,
        explorer: QWidget | None = None,
        project_key: str | None = None,
    ) -> None:
        key = project_key or label
        item = QListWidgetItem()
        row = _ProjectRow(
            label,
            collapsed=self._collapsed,
            explorer=explorer,
            expanded=self._load_explorer_expanded(key),
            project_key=key,
        )
        row.explorerToggled.connect(
            lambda expanded, r=row: self._on_row_explorer_toggled(r, expanded)
        )
        self._list.insertItem(index, item)
        self._list.setItemWidget(item, row)
        # Unlike QStackedWidget (auto-shows its first added widget),
        # QListWidget never auto-selects a row on insert — real QTabWidget
        # does auto-select its first added tab, and this class claims to be
        # QTabWidget-compatible, so replicate that here: if nothing is
        # selected yet, this new row becomes it. setCurrentRow (not a direct
        # set_selected call) so it runs through the normal
        # currentRowChanged -> _on_row_changed path — itemWidget is already
        # set at this point, so that path now finds this row correctly.
        if self._list.currentRow() < 0:
            self._list.setCurrentRow(index)
        # sizeHint is derived from the row's own layout (collapsed header-only
        # vs. header+expanded-tree) rather than a fixed QSize, since a row's
        # height now varies with whether its explorer is showing.
        item.setSizeHint(row.sizeHint())
