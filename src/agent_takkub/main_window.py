"""Main cockpit window.

Layout policy:
  - On startup: only the Lead pane is shown and fills the window.
  - When Lead does `takkub assign --role X`, the orchestrator emits
    `paneRequested("X")`. MainWindow creates an AgentPane for that role and
    adds it to the right-side vertical splitter, so the window grows to
    accommodate it (Lead shrinks).
  - When that role closes (`takkub done` or X button), the pane is removed.
    If no teammates remain, the right splitter collapses and Lead fills the
    window again.

This mimics tmux pane splitting but pre-arranged on a Lead | TeammateStack
axis — Lead always on the left, teammates stacked vertically on the right.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, QTimer, Qt
from PyQt6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QStyle,
    QWidget,
)

from .agent_pane import AgentPane
from .cli_server import CliServer
from .config import (
    EVENTS_LOG,
    active_project,
    list_project_names,
    preset_roles_for_active,
    set_active_project,
)
from .logs_panel import LogsPanel
from .orchestrator import Orchestrator
from .roles import DEFAULT_TEAMMATES, LEAD, Role, by_name


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("agent-takkub — dev team cockpit")
        self.resize(1500, 900)

        self._settings = QSettings("agent-takkub", "cockpit")
        self.setStyleSheet(
            "QMainWindow { background-color: #09090b; }"
            "QStatusBar { background: #18181b; color: #a1a1aa; }"
            "QSplitter::handle { background: #27272a; }"
            "QSplitter::handle:hover { background: #3f3f46; }"
        )

        # ── orchestrator + cli server ───────────────────────────
        self.orch = Orchestrator(self)
        self.cli = CliServer(self.orch, self)
        self.orch.paneRequested.connect(self._ensure_teammate_pane)
        self.orch.paneClosed.connect(self._remove_teammate_pane)
        self.orch.agentDone.connect(self._notify_agent_done)

        # ── system tray for desktop notifications ───────────────
        self._tray = QSystemTrayIcon(self)
        # use the OS-standard "information" icon — works without a bundled .ico
        self._tray.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
        self._tray.setToolTip("agent-takkub")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

        # ── root layout ─────────────────────────────────────────
        root = QWidget(self)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        self.setCentralWidget(root)

        # horizontal splitter: Lead | (teammate stack)
        self.main_split = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(self.main_split, 1)

        # Lead pane (always present)
        self.lead_pane = AgentPane(LEAD)
        self.orch.register_pane(self.lead_pane)
        self.main_split.addWidget(self.lead_pane)

        # teammate stack: vertical splitter that hosts teammate panes on demand
        self.teammate_split = QSplitter(Qt.Orientation.Vertical, self)
        self.teammate_split.setChildrenCollapsible(False)
        self.teammate_panes: dict[str, AgentPane] = {}
        # color overrides chosen via "+ pane → custom..." flow
        self._custom_role_colors: dict[str, str] = {}
        # Start hidden — Lead fills 100% until first teammate is added
        self.teammate_split.hide()
        self.main_split.addWidget(self.teammate_split)
        self.main_split.setSizes([1500, 0])

        # ── status bar ──────────────────────────────────────────
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("starting...")

        # right-side widgets: project combo + add-pane button
        self._project_combo = QComboBox(self)
        self._project_combo.setMinimumWidth(140)
        self._project_combo.setToolTip("Active project (used as default cwd)")
        self._refresh_project_list()
        self._project_combo.currentTextChanged.connect(self._on_project_changed)

        self._btn_add_pane = QPushButton("+ pane", self)
        self._btn_add_pane.setToolTip("Open a pane for a role (default or custom)")
        self._btn_add_pane.clicked.connect(self._on_add_pane_clicked)

        self._btn_assign = QPushButton("⟶ assign", self)
        self._btn_assign.setToolTip("Quick-assign a task to a role")
        self._btn_assign.clicked.connect(self._on_quick_assign_clicked)

        self._btn_logs = QPushButton("logs", self)
        self._btn_logs.setToolTip("Show/hide events log panel")
        self._btn_logs.setCheckable(True)
        self._btn_logs.clicked.connect(self._on_toggle_logs)

        self._btn_help = QPushButton("?", self)
        self._btn_help.setFixedWidth(28)
        self._btn_help.setToolTip("Show takkub command cheatsheet (F1)")
        self._btn_help.clicked.connect(self._show_help)

        self._status.addPermanentWidget(QLabel("project:"))
        self._status.addPermanentWidget(self._project_combo)
        self._status.addPermanentWidget(self._btn_add_pane)
        self._status.addPermanentWidget(self._btn_assign)
        self._status.addPermanentWidget(self._btn_logs)
        self._status.addPermanentWidget(self._btn_help)

        # ── bottom logs dock (hidden by default) ────────────────
        self._logs_dock = QDockWidget("events", self)
        self._logs_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._logs_dock.setWidget(LogsPanel(EVENTS_LOG))
        self._logs_dock.setMinimumHeight(140)
        self._logs_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._logs_dock)

        self.cli.started.connect(
            lambda port: self._status.showMessage(
                f"cockpit ready · cli port {port}"
            )
        )
        self.orch.statusChanged.connect(self._update_status)

        # Refresh status bar every 2s so the working/active count tracks the
        # state transitions that don't emit statusChanged (e.g. working→done
        # transitions inside orchestrator._send_when_ready).
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2_000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

        # ── boot: start CLI server + auto-spawn Lead ────────────
        QTimer.singleShot(0, self._boot)

        # Restore window/splitter sizes from last session (after layout is
        # built so the splitter has children to size).
        QTimer.singleShot(0, self._restore_window_state)

    # ──────────────────────────────────────────────────────────────
    # teammate pane lifecycle
    # ──────────────────────────────────────────────────────────────
    def _ensure_teammate_pane(self, role_name: str) -> None:
        if role_name == LEAD.name or role_name in self.teammate_panes:
            return
        role = by_name(role_name)
        if role is None:
            # custom role — use user-picked color if available, else default gray
            color = self._custom_role_colors.get(role_name, "#94a3b8")
            role = Role(
                name=role_name,
                label=role_name.capitalize(),
                color=color,
                column=2,
                row=99,
            )

        pane = AgentPane(role)
        # close routing: AgentPane.closeRequested → orchestrator.close (via
        # the connection set up in register_pane) → orchestrator.paneClosed
        # → _remove_teammate_pane. One signal chain, no race.
        self.teammate_panes[role_name] = pane
        self.orch.register_pane(pane)
        self.teammate_split.addWidget(pane)

        # show right side and rebalance
        if not self.teammate_split.isVisible():
            self.teammate_split.show()
            self.main_split.setSizes([900, 600])
        self._rebalance_teammates()
        self._status.showMessage(f"added pane · {role_name}", 4_000)

    def _remove_teammate_pane(self, role_name: str) -> None:
        pane = self.teammate_panes.pop(role_name, None)
        if pane is None:
            return
        self.orch.unregister_pane(role_name)
        pane.setParent(None)
        pane.deleteLater()
        if not self.teammate_panes:
            self.teammate_split.hide()
            self.main_split.setSizes([self.width(), 0])
        else:
            self._rebalance_teammates()

    def _rebalance_teammates(self) -> None:
        n = self.teammate_split.count()
        if n <= 0:
            return
        h = max(self.teammate_split.height(), 100)
        each = max(120, h // n)
        self.teammate_split.setSizes([each] * n)

    # ──────────────────────────────────────────────────────────────
    def _boot(self) -> None:
        try:
            port = self.cli.listen()
            self._status.showMessage(f"cli port {port}, spawning Lead...")
        except Exception as e:
            self._status.showMessage(f"⚠ cli server failed: {e}", 15_000)

        # Reflect orchestrator errors (eg. claude.exe not found) into the
        # status bar instead of swallowing them silently.
        self.orch.paneRequested.connect(self._track_pane_request)

        ok, msg = self.orch.spawn(LEAD.name)
        if not ok:
            self._status.showMessage(f"⚠ Lead spawn failed: {msg}", 30_000)
        # Surface label "Lead · <project>" so the user always knows which
        # project's defaults apply when they assign work.
        active = active_project()[0]
        if active:
            self.lead_pane._title.setText(f"Lead · {active}")

        # Auto-spawn project presets after Lead has had a moment to boot.
        # Stagger 3s apart so we don't hammer the system or race on auto-trust.
        presets = preset_roles_for_active()
        if presets:
            self._status.showMessage(
                f"auto-spawning {len(presets)} preset role(s): {', '.join(presets)}",
                6_000,
            )
            for i, role in enumerate(presets):
                QTimer.singleShot(
                    15_000 + i * 3_000, lambda r=role: self.orch.spawn(r)
                )

    def _track_pane_request(self, role_name: str) -> None:
        # only called when a new pane is being created
        self._status.showMessage(f"opening pane · {role_name}", 3_000)

    def _notify_agent_done(self, role_name: str, note: str) -> None:
        """Show a desktop toast when an agent reports done."""
        title = f"{role_name} done"
        body = note.strip() if note else "task complete"
        # mirror to status bar too in case tray is unavailable
        self._status.showMessage(f"✓ {title}: {body[:80]}", 6_000)
        if self._tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(
                title, body, QSystemTrayIcon.MessageIcon.Information, 5_000
            )

    # ──────────────────────────────────────────────────────────────
    # toolbar buttons
    # ──────────────────────────────────────────────────────────────
    def _on_toggle_logs(self, checked: bool) -> None:
        self._logs_dock.setVisible(checked)

    def _on_quick_assign_clicked(self) -> None:
        taken = list(self.orch.panes.keys())
        defaults = [r.name for r in DEFAULT_TEAMMATES]
        choices = defaults + [r for r in taken if r not in defaults and r != LEAD.name]
        if not choices:
            choices = defaults
        role, ok = QInputDialog.getItem(
            self, "Assign task", "Role:", choices, 0, False
        )
        if not ok or not role:
            return
        task, ok = QInputDialog.getMultiLineText(
            self, "Assign task", f"Task for {role}:", ""
        )
        if not ok or not task.strip():
            return
        ok, msg = self.orch.assign(role, cwd=None, task=task.strip())
        self._status.showMessage(msg, 4_000)

    def _show_help(self) -> None:
        from PyQt6.QtWidgets import QMessageBox

        text = (
            "<b>takkub CLI</b> (run from Lead pane or any agent pane):<br><br>"
            "<code>takkub list</code> — show pane status<br>"
            "<code>takkub spawn --role &lt;name&gt; [--cwd &lt;path&gt;]</code> — open a pane<br>"
            "<code>takkub assign --role &lt;name&gt; [--cwd &lt;path&gt;] \"task\"</code> — spawn + send task<br>"
            "<code>takkub send --to &lt;role&gt; \"msg\"</code> — peer message (Lead is CC'd)<br>"
            "<code>takkub close --role &lt;name&gt;</code> — close a pane<br>"
            "<code>takkub close-all</code> — close every teammate (keeps Lead)<br>"
            "<code>takkub done [note]</code> — (agents) signal completion<br><br>"
            "<b>Shortcuts</b><br>"
            "Ctrl + + / - / 0 — terminal font size (per pane)<br>"
            "Wheel — scroll claude's history (PgUp/PgDn passthrough)<br>"
            "F1 — this dialog<br><br>"
            "<b>Default cwd</b> when <code>--cwd</code> omitted: active project's<br>"
            "role-matched path (frontend→web, backend→api, ...)."
        )
        QMessageBox.information(self, "agent-takkub help", text)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_F1:
            self._show_help()
            return
        super().keyPressEvent(event)

    def _update_status(self) -> None:
        active = 0
        working = 0
        for p in self.orch.panes.values():
            if p.session is not None and p.session.is_alive:
                active += 1
                if p.state == "working":
                    working += 1
        port = self.cli._server.serverPort() if self.cli._server.isListening() else 0
        bits = [f"cockpit · cli {port}", f"{active} active"]
        if working:
            bits.append(f"{working} working")
        self._status.showMessage("  ·  ".join(bits))

    # ──────────────────────────────────────────────────────────────
    # project switcher
    # ──────────────────────────────────────────────────────────────
    def _refresh_project_list(self) -> None:
        self._project_combo.blockSignals(True)
        try:
            self._project_combo.clear()
            names = list_project_names()
            self._project_combo.addItems(names)
            cur = active_project()[0]
            if cur:
                idx = self._project_combo.findText(cur)
                if idx >= 0:
                    self._project_combo.setCurrentIndex(idx)
        finally:
            self._project_combo.blockSignals(False)

    def _on_project_changed(self, name: str) -> None:
        if not name:
            return
        if set_active_project(name):
            self._status.showMessage(f"active project → {name}", 4_000)
            self.lead_pane._title.setText(f"Lead · {name}")

    # ──────────────────────────────────────────────────────────────
    # + pane button: spawn an additional teammate from a role name
    # ──────────────────────────────────────────────────────────────
    def _on_add_pane_clicked(self) -> None:
        # roles already shown (skip them from the picker)
        taken = set(self.orch.panes.keys())
        default_unused = [
            r.name for r in DEFAULT_TEAMMATES if r.name not in taken
        ]
        choices = default_unused + ["custom..."]
        name, ok = QInputDialog.getItem(
            self,
            "Open pane",
            "Choose a role (or pick 'custom...' for a new name):",
            choices,
            0,
            False,
        )
        if not ok or not name:
            return
        if name == "custom...":
            name, ok = QInputDialog.getText(
                self, "Custom role", "Role name (lowercase, no spaces):"
            )
            if not ok or not name:
                return
            name = name.strip().lower().replace(" ", "-")
            if not name:
                return
            # let the user pick a colour for the dot indicator
            picker = QColorDialog(self)
            picker.setWindowTitle(f"Color for role '{name}'")
            picker.setCurrentColor(Qt.GlobalColor.cyan)
            if picker.exec():
                self._custom_role_colors[name] = picker.currentColor().name()
        ok, msg = self.orch.spawn(name)
        self._status.showMessage(msg, 4_000)

    # ──────────────────────────────────────────────────────────────
    # window state persistence
    # ──────────────────────────────────────────────────────────────
    def _restore_window_state(self) -> None:
        geo = self._settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        main_sizes = self._settings.value("split/main")
        if main_sizes:
            try:
                self.main_split.setSizes([int(x) for x in main_sizes])
            except (TypeError, ValueError):
                pass

    def _save_window_state(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("split/main", self.main_split.sizes())
        self._settings.sync()

    # ──────────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_window_state()
        for pane in list(self.orch.panes.values()):
            if pane.session is not None:
                pane.mark_expected_exit()
                pane.session.terminate()
        self.cli.close()
        super().closeEvent(event)


# DEFAULT_TEAMMATES is imported only so the symbol stays referenced (for the
# CLI tab-completion / future role picker UI). Suppress unused warning.
_ = DEFAULT_TEAMMATES
