"""ProjectTab: one project's panes, shown as a tab strip.

Layout (2026-06-26 redesign — presentation only, no engine change):
  projects live in MainWindow's left **sidebar** (`ProjectNav`); each ProjectTab
  is the right-hand content for one project. Inside a ProjectTab every pane —
  Lead + each teammate — is a **tab** in a `QTabWidget`, so the user sees one
  full-size pane at a time and switches with the tab strip (was: Lead-left +
  teammate vertical splitter grid).

This pairs with the tab-visibility keep-alive (only the *visible* pane in the
*visible* project paints; everything else suspends so Chromium can reclaim
compositor RAM) and adds an unread **red dot** on the Lead tab when a notice
arrives while the user is looking at another pane.

Wiring:
  ProjectTab.project_name   — string, immutable after construction
  ProjectTab.lead_pane      — the Lead AgentPane (always pane-tab 0)
  ProjectTab.pane_tabs      — QTabWidget holding every pane
  ProjectTab.teammate_panes — dict[role_name → AgentPane]
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from . import cockpit_theme
from .agent_pane import AgentPane

# Shared icon canvas for every pane-tab icon (unread-dot + role/status combo)
# — QTabWidget.setIconSize is tab-bar-wide, so every icon painted onto a tab
# must share one size or Qt rescales whichever was set last.
_TAB_ICON_SIZE = QSize(16, 10)

# Cluster B #1 (UI walkthrough 2026-07-11, "Per-pane-tab status dot") — a
# Multi-mode fan-out spawns several teammate panes at once, and there was no
# way to tell working/done apart without clicking into each tab. AgentPane
# already tracks this as a plain `.state` attribute (empty/active/working/
# done/exited/error, see agent_pane.py) but emits no change signal, and
# agent_pane.py is out of scope for this task — so the tab dot is refreshed
# by a light poll timer instead of a push signal.
_TAB_STATUS_COLORS = {
    "working": cockpit_theme.STATE_WARN_BRIGHT,  # yellow — actively running
    "done": cockpit_theme.STATE_OK_BRIGHT,  # green — finished
}
_TAB_STATUS_DEFAULT = cockpit_theme.TEXT_FAINT  # idle/active/empty — grey
_TAB_STATUS_POLL_MS = 600

# Modern flat tab strip for the panes inside a project. Selected accent = gold
# (the design system's one active accent — was indigo #6366f1).
_PANE_TABS_QSS = f"""
QTabWidget::pane {{
    border: none;
    background: {cockpit_theme.GROUND_SIDEBAR};
}}
QTabBar {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: transparent;
    color: {cockpit_theme.TEXT_MUTED};
    padding: 7px 16px;
    margin: 0;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 12px;
}}
QTabBar::tab:hover {{
    color: {cockpit_theme.TEXT_SECONDARY};
    background: {cockpit_theme.GROUND_PANEL};
}}
QTabBar::tab:selected {{
    color: {cockpit_theme.TEXT_PRIMARY};
    border-bottom: 2px solid {cockpit_theme.ACCENT_GOLD};
    background: {cockpit_theme.GROUND_PANEL};
}}
"""


class ProjectTab(QWidget):
    """Tabbed stack of one project's panes.

    Construct with `lead_pane=None` and pass the tab through the sidebar's
    `addTab` *before* attaching the Lead via `attach_lead()`. The deferred
    attach keeps the Lead's QWebEngineView from being re-parented after its
    first paint (which crashes Chromium's renderer on Windows).
    """

    # Emitted when the user closes a teammate pane-tab via its × button. Carries
    # the role name; MainWindow routes it through the orchestrator's close chain
    # (same path as the pane header's own × button), which calls back into
    # remove_teammate_tab — so there is exactly one teardown path, no race.
    paneCloseRequested = pyqtSignal(str)

    def __init__(
        self,
        project_name: str,
        lead_pane: AgentPane | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_name = project_name
        self.lead_pane: AgentPane | None = None

        # Whether this tab is the currently visible project (set by MainWindow
        # on every sidebar switch). Combined with the per-pane current-tab
        # check so only the visible pane of the visible project paints.
        self._keepalive = True

        self.pane_tabs = QTabWidget(self)
        self.pane_tabs.setDocumentMode(True)
        self.pane_tabs.setMovable(True)
        self.pane_tabs.setTabsClosable(True)
        self.pane_tabs.setStyleSheet(_PANE_TABS_QSS)
        self.pane_tabs.tabCloseRequested.connect(self._on_pane_tab_close)
        self.pane_tabs.currentChanged.connect(self._on_pane_tab_changed)

        self.teammate_panes: dict[str, AgentPane] = {}
        # Color overrides chosen via the "+ pane → custom..." flow stay scoped
        # to the tab so two projects can use different palettes independently.
        self.custom_role_colors: dict[str, str] = {}

        # Cached red-dot icon for the unread Lead indicator (built lazily).
        self._unread_icon: QIcon | None = None
        # Cached role-color + status-color combo icons for teammate tabs,
        # keyed by (role_color, status_color) so repeat states reuse one QIcon.
        self._tab_status_icons: dict[tuple[str, str], QIcon] = {}
        self.pane_tabs.setIconSize(_TAB_ICON_SIZE)

        # Poll teammate pane `.state` and repaint each tab's status dot —
        # see the _TAB_STATUS_* module comment for why this is a poll, not a
        # signal. Cheap: skips entirely when there are no teammate tabs.
        self._tab_status_timer = QTimer(self)
        self._tab_status_timer.setInterval(_TAB_STATUS_POLL_MS)
        self._tab_status_timer.timeout.connect(self._refresh_teammate_tab_icons)
        self._tab_status_timer.start()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.pane_tabs)

        if lead_pane is not None:
            # Backwards-compat path for callers that still pass Lead at
            # construction time (e.g. tests).
            self.attach_lead(lead_pane)

    # ------------------------------------------------------------------
    # pane tabs
    # ------------------------------------------------------------------
    def mount_usage_widget(self, widget: QWidget) -> None:
        """Park the usage/limit meter QLabel in the top-right corner of pane_tabs.

        Called by MainWindow once at boot (initial tab) and again on every
        project switch so the single label follows the active ProjectTab.
        Qt automatically removes the widget from any previous corner when
        setCornerWidget is called on a new QTabWidget.
        """
        self.pane_tabs.setCornerWidget(widget, Qt.Corner.TopRightCorner)
        widget.show()

    def attach_lead(self, lead_pane: AgentPane) -> None:
        """Insert the Lead pane as the first (non-closable) tab. Idempotent
        but only meant to be called once per tab."""
        if self.lead_pane is not None:
            return
        self.lead_pane = lead_pane
        self.pane_tabs.insertTab(0, lead_pane, "Lead")
        # Lead is never closable — strip its × so the only way out is closing
        # the whole project from the sidebar.
        self._strip_tab_close_button(0)
        self.pane_tabs.setCurrentIndex(0)
        self._apply_pane_keepalive()

    def add_teammate_tab(self, role_name: str, pane: AgentPane, label: str) -> None:
        """Register a teammate pane, add it as a tab, and **switch to it** so the
        freshly-spawned agent is visible immediately — the panes-as-tabs
        equivalent of the old grid where a new pane appeared on screen. Without
        this the new tab is a hidden background tab and a spawn looks like
        nothing happened. setCurrentIndex fires `_on_pane_tab_changed` →
        `_apply_pane_keepalive`, so the new pane also resumes painting."""
        self.teammate_panes[role_name] = pane
        idx = self.pane_tabs.addTab(pane, label)
        self.pane_tabs.setCurrentIndex(idx)
        self._apply_pane_keepalive()
        # Paint the new tab's role/status dot immediately — don't make the
        # user wait out the poll interval to see a just-spawned pane's dot.
        self._refresh_teammate_tab_icons()

    def remove_teammate_tab(self, role_name: str) -> AgentPane | None:
        """Drop a teammate pane's tab and registry entry. Returns the pane so
        the caller can tear down its WebEngine view, or None if not present."""
        pane = self.teammate_panes.pop(role_name, None)
        if pane is None:
            return None
        idx = self.pane_tabs.indexOf(pane)
        if idx >= 0:
            self.pane_tabs.removeTab(idx)
        self._apply_pane_keepalive()
        return pane

    def has_teammates(self) -> bool:
        return bool(self.teammate_panes)

    def _strip_tab_close_button(self, index: int) -> None:
        bar = self.pane_tabs.tabBar()
        for side in (bar.ButtonPosition.RightSide, bar.ButtonPosition.LeftSide):
            btn = bar.tabButton(index, side)
            if btn is not None:
                btn.deleteLater()
            bar.setTabButton(index, side, None)

    def _on_pane_tab_close(self, index: int) -> None:
        w = self.pane_tabs.widget(index)
        if w is None or w is self.lead_pane:
            return  # Lead is never closable here
        role = getattr(getattr(w, "role", None), "name", None)
        if role:
            # Reuse the pane's own close chain (orchestrator.close → paneClosed
            # → remove_teammate_tab) instead of tearing down here directly.
            self.paneCloseRequested.emit(role)

    # ------------------------------------------------------------------
    # keep-alive: only the visible pane of the visible project paints
    # ------------------------------------------------------------------
    def set_keepalive(self, active: bool) -> None:
        """Mark this project visible/hidden. MainWindow calls it on every
        sidebar switch (visible project → True, others → False). Idempotent."""
        self._keepalive = bool(active)
        self._apply_pane_keepalive()

    def _apply_pane_keepalive(self) -> None:
        """A pane paints iff this project is visible AND it's the current
        pane-tab. Everything else suspends so its renderer can release RAM."""
        cur = self.pane_tabs.currentWidget()
        for i in range(self.pane_tabs.count()):
            w = self.pane_tabs.widget(i)
            setter = getattr(w, "set_keepalive", None)
            if setter is not None:
                setter(self._keepalive and w is cur)
        # If the Lead tab is the one on screen, it's been seen → clear its dot.
        if self._keepalive and cur is self.lead_pane:
            self._clear_lead_unread()

    def _on_pane_tab_changed(self, _index: int) -> None:
        self._apply_pane_keepalive()

    # ------------------------------------------------------------------
    # per-teammate-tab role color + status dot (UI walkthrough #43/#46)
    # ------------------------------------------------------------------
    def _tab_status_icon(self, role_color: str, status_color: str) -> QIcon:
        """Two small dots side by side: role identity (left) + working/done/
        idle state (right). Cached per color pair so the poll timer doesn't
        rebuild a QPixmap every tick for panes whose state hasn't changed."""
        key = (role_color, status_color)
        icon = self._tab_status_icons.get(key)
        if icon is not None:
            return icon
        pix = QPixmap(_TAB_ICON_SIZE)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(role_color))
        p.drawEllipse(0, 1, 6, 6)
        p.setBrush(QColor(status_color))
        p.drawEllipse(9, 1, 6, 6)
        p.end()
        icon = QIcon(pix)
        self._tab_status_icons[key] = icon
        return icon

    def _refresh_teammate_tab_icons(self) -> None:
        """Repaint every teammate pane-tab's role/status dot from the live
        pane state. Cheap no-op when there are no teammate tabs."""
        if not self.teammate_panes:
            return
        for pane in self.teammate_panes.values():
            idx = self.pane_tabs.indexOf(pane)
            if idx < 0:
                continue
            role = getattr(pane, "role", None)
            role_color = getattr(role, "color", None) or cockpit_theme.ROLE_COLOR_FALLBACK
            status_color = _TAB_STATUS_COLORS.get(getattr(pane, "state", None), _TAB_STATUS_DEFAULT)
            self.pane_tabs.setTabIcon(idx, self._tab_status_icon(role_color, status_color))

    # ------------------------------------------------------------------
    # unread red dot on the Lead tab
    # ------------------------------------------------------------------
    def _dot_icon(self) -> QIcon:
        if self._unread_icon is None:
            pix = QPixmap(_TAB_ICON_SIZE)
            pix.fill(Qt.GlobalColor.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(cockpit_theme.STATE_ERROR))
            p.drawEllipse(3, 1, 8, 8)
            p.end()
            self._unread_icon = QIcon(pix)
        return self._unread_icon

    def mark_lead_unread(self) -> None:
        """Put an unread red dot on the Lead pane-tab — unless the user is
        already looking at it (visible project + Lead tab current)."""
        if self.lead_pane is None:
            return
        idx = self.pane_tabs.indexOf(self.lead_pane)
        if idx < 0:
            return
        if self._keepalive and self.pane_tabs.currentWidget() is self.lead_pane:
            return  # already on screen — nothing unseen
        self.pane_tabs.setTabIcon(idx, self._dot_icon())

    def _clear_lead_unread(self) -> None:
        if self.lead_pane is None:
            return
        idx = self.pane_tabs.indexOf(self.lead_pane)
        if idx >= 0:
            self.pane_tabs.setTabIcon(idx, QIcon())
