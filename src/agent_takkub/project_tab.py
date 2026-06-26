"""ProjectTab: one project's pane stack, hosted inside MainWindow's QTabWidget.

Each tab owns its own Lead pane and a vertical splitter of teammate panes.
Status bar buttons (Add Agent / Assign Task / Finish Job / Install rtk)
are shared at the MainWindow level and always act on the *currently
active* tab, resolved via QTabWidget.currentWidget(). The cockpit refuses
to open the same project twice — one tab per project, strictly.

Wiring:
  ProjectTab.project_name   — string, immutable after construction
  ProjectTab.lead_pane      — the AgentPane sitting in column 0
  ProjectTab.teammate_split — vertical QSplitter holding teammate panes
  ProjectTab.teammate_panes — dict[role_name → AgentPane] for that tab
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QSplitter, QWidget

from .agent_pane import AgentPane


class ProjectTab(QWidget):
    """Visual stack of one project's panes.

    Construct with `lead_pane=None` and pass the tab through
    `QTabWidget.addTab` *before* attaching the Lead via `attach_lead()`.
    The deferred attach pattern keeps QWebEngineView from being
    re-parented under a freshly-added tab — which crashes Chromium's
    renderer on Windows during the first paint.
    """

    def __init__(
        self,
        project_name: str,
        lead_pane: AgentPane | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_name = project_name
        self.lead_pane: AgentPane | None = None

        # Horizontal split: Lead on the left, teammate stack on the right.
        self.main_split = QSplitter(Qt.Orientation.Horizontal, self)

        self.teammate_split = QSplitter(Qt.Orientation.Vertical, self)
        self.teammate_split.setChildrenCollapsible(False)
        self.teammate_split.hide()  # Lead fills 100% until first teammate

        # Splitter starts with two empty slots — Lead is attached later
        # via `attach_lead()` so its WebEngineView never sees a parent
        # change after first paint.
        self.main_split.addWidget(self.teammate_split)

        self.teammate_panes: dict[str, AgentPane] = {}
        # Whether this tab is the currently visible one. MainWindow flips this
        # via set_keepalive() on every tab switch so panes in hidden tabs
        # suspend their paint keep-alive and release Chromium compositor RAM.
        # Stored so a pane attached while the tab is hidden inherits the right
        # state instead of painting (and leaking) until the next switch.
        self._keepalive = True
        # Color overrides chosen via the "+ pane → custom..." flow stay
        # scoped to the tab so two projects can use different palettes
        # without clobbering each other.
        self.custom_role_colors: dict[str, str] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.main_split)

        if lead_pane is not None:
            # Backwards-compat path for callers that still pass Lead at
            # construction time (e.g. tests). Same widget order as the
            # original implementation.
            self.attach_lead(lead_pane)

    def attach_lead(self, lead_pane: AgentPane) -> None:
        """Insert the Lead pane on the splitter's left side. Idempotent
        but only meant to be called once per tab."""
        if self.lead_pane is not None:
            return
        self.lead_pane = lead_pane
        self.main_split.insertWidget(0, lead_pane)
        self.main_split.setSizes([1500, 0])
        # Inherit the tab's current visibility so a Lead attached into a
        # hidden tab starts suspended rather than painting until next switch.
        if not self._keepalive:
            lead_pane.set_keepalive(False)

    def rebalance_teammates(self) -> None:
        """Distribute vertical space evenly across teammate panes."""
        n = self.teammate_split.count()
        if n <= 0:
            return
        h = max(self.teammate_split.height(), 100)
        each = max(120, h // n)
        self.teammate_split.setSizes([each] * n)

    def set_keepalive(self, active: bool) -> None:
        """Suspend/resume background paint keep-alive for every pane in this tab.

        MainWindow calls this on each tab switch: the visible tab gets True,
        every hidden tab gets False. Hidden panes stop forcing repaints so
        Chromium can release their renderer's compositor memory (the fix for
        backgrounded-tab renderers ballooning to multi-GB). Idempotent."""
        self._keepalive = bool(active)
        if self.lead_pane is not None:
            self.lead_pane.set_keepalive(self._keepalive)
        for pane in self.teammate_panes.values():
            pane.set_keepalive(self._keepalive)

    def has_teammates(self) -> bool:
        return bool(self.teammate_panes)

    def show_teammate_split(self) -> None:
        if not self.teammate_split.isVisible():
            self.teammate_split.show()
            # restore a usable lead/teammates split (~62/38) when the right
            # column comes alive for the first time
            self.main_split.setSizes([900, 600])

    def hide_teammate_split(self) -> None:
        self.teammate_split.hide()
        self.main_split.setSizes([self.width(), 0])
