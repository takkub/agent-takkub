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

import os
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QWidget,
)

from .agent_pane import AgentPane
from .cli_server import CliServer
from .config import (
    EVENTS_LOG,
    active_project,
    get_open_tabs,
    lead_cwd,
    list_project_names,
    preset_roles_for_active,
    set_active_project,
    set_open_tabs,
)
from .logs_panel import LogsPanel
from .orchestrator import Orchestrator
from .project_tab import ProjectTab
from .roles import DEFAULT_TEAMMATES, LEAD, Role, by_name
from .rtk_helper import install_rtk, is_rtk_installed, rtk_binary_available
from .token_meter import format_tokens, usage_color


class MainWindow(QMainWindow):
    # ──────────────────────────────────────────────────────────────
    # backwards-compat accessors: until every callsite is rewritten to
    # explicitly target a project tab, these resolve to the *current*
    # tab's widgets so legacy methods keep working in single-tab mode.
    # ──────────────────────────────────────────────────────────────
    def _current_tab(self) -> ProjectTab:
        """Return the currently focused ProjectTab. Caller must not hold
        the reference across tab switches."""
        return self.tabs.currentWidget()

    @property
    def lead_pane(self) -> AgentPane:
        return self._current_tab().lead_pane

    @property
    def teammate_panes(self) -> dict[str, AgentPane]:
        return self._current_tab().teammate_panes

    @property
    def teammate_split(self):
        return self._current_tab().teammate_split

    @property
    def main_split(self):
        return self._current_tab().main_split

    @property
    def _custom_role_colors(self) -> dict[str, str]:
        return self._current_tab().custom_role_colors

    def _rebalance_teammates(self) -> None:
        self._current_tab().rebalance_teammates()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("agent-takkub — dev team cockpit")

        icon_path = Path(__file__).parent.parent.parent / "assets" / "icon.png"
        app_icon = (
            QIcon(str(icon_path))
            if icon_path.exists()
            else self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
        self.setWindowIcon(app_icon)

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
        self._tray.setIcon(app_icon)
        self._tray.setToolTip("agent-takkub")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

        # ── root layout ─────────────────────────────────────────
        root = QWidget(self)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        self.setCentralWidget(root)

        # Central QTabWidget hosts one ProjectTab per open project. The
        # cockpit enforces strict one-tab-per-project. Helper accessors
        # below (`self.lead_pane`, `self.teammate_split`, ...) resolve to
        # the *currently active* tab so older MainWindow methods keep
        # operating on the same widget set without explicit project
        # plumbing.
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(False)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.tabBarClicked.connect(self._on_tab_bar_clicked)
        # NOTE: `currentChanged` is connected at the end of __init__ — once
        # the status bar widgets exist. QTabWidget emits currentChanged the
        # moment the first tab is added, and our slot calls
        # `_refresh_rtk_button` which touches `self._btn_install_rtk`.
        # Connecting up-front means the slot fires while the button still
        # doesn't exist, raising AttributeError inside a Qt slot — which
        # the underlying event-dispatch path then surfaces as a silent
        # Chromium renderer crash on Windows (pythonw shows nothing; the
        # cockpit window never appears).

        outer.addWidget(self.tabs, 1)

        # Build the initial tab for the active project. Order matters
        # here: QTabWidget.addTab re-parents the inserted widget under
        # its internal stacked widget. If a QWebEngineView is already
        # nested inside the tab when that re-parent happens, Chromium's
        # renderer process crashes silently on Windows ("composition
        # surface invalidated"). Adding the tab BEFORE creating the
        # AgentPane that owns the WebEngineView keeps the chain stable
        # from the QWebEngineView's first paint onward.
        initial_project = active_project()[0] or "default"
        initial_tab = ProjectTab(initial_project, lead_pane=None)
        self.tabs.addTab(initial_tab, initial_project)
        initial_lead = AgentPane(LEAD, parent=initial_tab)
        self.orch.register_pane(initial_lead, project=initial_project)
        initial_tab.attach_lead(initial_lead)

        # Append a permanent "+" pseudo-tab at the end of the strip. It
        # has no widget content (an empty QWidget placeholder), is not
        # closable, and intercepts clicks via `_on_tab_bar_clicked` to
        # show the project picker instead of activating itself. Real new
        # tabs always insert *before* the "+" via `_plus_tab_index()` so
        # the "+" stays anchored on the right.
        from PyQt6.QtWidgets import QWidget as _QW

        self._plus_tab_placeholder = _QW(self)
        plus_idx = self.tabs.addTab(self._plus_tab_placeholder, "+")
        # Strip the close-button from the "+" tab; it must always be there.
        bar = self.tabs.tabBar()
        bar.setTabButton(plus_idx, bar.ButtonPosition.RightSide, None)
        bar.setTabButton(plus_idx, bar.ButtonPosition.LeftSide, None)
        self.tabs.setTabToolTip(plus_idx, "Open another project in a new tab")
        # Restore focus to the real first tab — addTab on the "+" would
        # otherwise make it the active tab (and Lead's pane would hide).
        self.tabs.setCurrentIndex(0)

        # ── status bar ──────────────────────────────────────────
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("starting...")

        self._project_combo = QComboBox(self)
        self._project_combo.setMinimumWidth(140)
        self._project_combo.setToolTip("Active project (used as default cwd)")
        self._refresh_project_list()
        self._project_combo.currentTextChanged.connect(self._on_project_changed)

        self._btn_add_project = QPushButton("📁", self)
        self._btn_add_project.setToolTip("Add new project from folder")
        self._btn_add_project.setFixedWidth(28)
        self._btn_add_project.clicked.connect(self._on_add_project_clicked)

        # One-click rtk install for the active project. Only visible when
        # the project hasn't been initialised yet — once `.claude/settings.json`
        # carries the Bash hook, the button hides itself so it never nags.
        # Detection runs on startup, on project switch, and after install.
        self._btn_install_rtk = QPushButton("⚡ Install rtk", self)
        self._btn_install_rtk.setToolTip(
            "Add the rtk PreToolUse Bash hook to this project's .claude/settings.json\n"
            "so every Bash tool call gets auto-rewritten with rtk (60-90% token savings\n"
            "on git / docker / npm / pytest / next / prisma output)."
        )
        self._btn_install_rtk.setStyleSheet(
            "QPushButton { color: #000; background: #fbbf24; "
            "border: 2px solid #b45309; border-radius: 4px; "
            "padding: 4px 12px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background: #fcd34d; }"
        )
        self._btn_install_rtk.clicked.connect(self._on_install_rtk_clicked)

        self._btn_add_pane = QPushButton("➕ Add Agent", self)
        self._btn_add_pane.setToolTip("Open a pane for a role (default or custom)")
        self._btn_add_pane.clicked.connect(self._on_add_pane_clicked)

        self._btn_assign = QPushButton("📝 Assign Task", self)
        self._btn_assign.setToolTip("Quick-assign a task to a role")
        self._btn_assign.clicked.connect(self._on_quick_assign_clicked)

        self._btn_logs = QPushButton("📋 Logs", self)
        self._btn_logs.setToolTip("Show/hide events log panel")
        self._btn_logs.setCheckable(True)
        self._btn_logs.clicked.connect(self._on_toggle_logs)

        self._btn_finish = QPushButton("✅ Finish Job", self)
        self._btn_finish.setToolTip(
            "Mark this whole session as done. Asks Lead to write a final\n"
            "summary (what was accomplished, what's blocked, next steps),\n"
            "then closes every teammate pane. Lead stays running so you\n"
            "can read the summary or start a new task."
        )
        self._btn_finish.setStyleSheet(
            "QPushButton { color: #14532d; background: #86efac; "
            "border: 1px solid #16a34a; border-radius: 4px; "
            "padding: 2px 8px; font-weight: 500; }"
            "QPushButton:hover { background: #bbf7d0; }"
        )
        self._btn_finish.clicked.connect(self._on_finish_job_clicked)

        self._btn_help = QPushButton("❓ Help", self)
        self._btn_help.setFixedWidth(70)
        self._btn_help.setToolTip("Show takkub command cheatsheet (F1)")
        self._btn_help.clicked.connect(self._show_help)

        # Persistent /remote-control reminder. The built-in Claude Code
        # command bridges a local session to claude.ai/code for browser/phone
        # control. The user routinely forgets it exists, so we keep a small
        # always-visible chip in the status bar that names it explicitly.
        self._remote_hint = QLabel("💡 /remote-control → control from browser", self)
        self._remote_hint.setStyleSheet(
            "color: #fbbf24; font-size: 11px; padding: 0 8px; "
            "background: rgba(251, 191, 36, 0.08); border-radius: 4px;"
        )
        self._remote_hint.setToolTip(
            "Run /remote-control inside the Lead pane to bridge this Claude\n"
            "session to claude.ai/code. Lets you continue from a browser or\n"
            "phone. Built-in Claude Code feature (2.x+)."
        )

        # Aggregate token meter: sums prompt tokens across every active pane
        # so the user can spot when the whole team is bumping the limit.
        # Tooltip breaks it down per role + reveals the largest occupant.
        self._token_total = QLabel("", self)
        self._token_total.setToolTip("Aggregate context occupancy across all active panes")
        self._token_total.setStyleSheet(
            "color: #6b7280; font-size: 11px; padding: 0 6px; font-variant-numeric: tabular-nums;"
        )
        self._token_total.hide()

        # Per-pane "context >= 80%" warning state. Keyed
        # `<project>::<role>`. We toast (status bar + tray) the first
        # time a pane crosses 80% and then stay silent until it dips
        # below 70% — the gap stops a pane that's hovering around
        # 80% from spamming notifications.
        self._context_warned: dict[str, bool] = {}

        self._status.addPermanentWidget(self._remote_hint)
        self._status.addPermanentWidget(self._token_total)
        # `project_combo` is retained as a hidden widget so legacy code
        # paths that update it (`_refresh_project_list`, `_on_project_changed`)
        # still link cleanly. The tab strip is now the authoritative
        # project switcher — the combo would just duplicate it visually.
        self._project_combo.hide()
        self._status.addPermanentWidget(self._btn_add_project)
        self._status.addPermanentWidget(self._btn_install_rtk)
        self._status.addPermanentWidget(self._btn_add_pane)
        self._status.addPermanentWidget(self._btn_assign)
        self._status.addPermanentWidget(self._btn_logs)
        self._status.addPermanentWidget(self._btn_finish)
        self._status.addPermanentWidget(self._btn_help)
        # Sync rtk button visibility after every permanent widget has been
        # added, so any layout invalidation triggered by show()/hide() lands
        # on a fully-built status bar rather than a half-built one (an
        # earlier mid-loop call kept the button invisible on first paint).
        self._refresh_rtk_button()

        # Only NOW is it safe to listen for tab switches — the handler
        # touches `_btn_install_rtk` via `_refresh_rtk_button`, which
        # didn't exist when the first tab was added. See the comment
        # next to the deferred connection at MW.11.
        self.tabs.currentChanged.connect(self._on_tab_switched)

        # ── bottom logs dock (hidden by default) ────────────────
        self._logs_dock = QDockWidget("events", self)
        self._logs_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._logs_dock.setWidget(LogsPanel(EVENTS_LOG))
        self._logs_dock.setMinimumHeight(140)
        self._logs_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._logs_dock)

        self.cli.started.connect(
            lambda port: self._status.showMessage(f"cockpit ready · cli port {port}")
        )
        self.orch.statusChanged.connect(self._update_status)

        # Refresh status bar every 2s so the working/active count tracks the
        # state transitions that don't emit statusChanged (e.g. working→done
        # transitions inside orchestrator._send_when_ready).
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2_000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

        # Periodic session snapshot so a hard crash (Alt+F4 mishandled,
        # power loss, force-kill from Task Manager) still leaves a recent
        # picture on disk for the next launch. closeEvent writes one final
        # time on a clean shutdown.
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setInterval(60_000)
        self._session_save_timer.timeout.connect(self.orch.write_session_snapshot)
        self._session_save_timer.start()

        # ── boot: start CLI server + auto-spawn Lead ────────────
        QTimer.singleShot(0, self._boot)

        # Restore window/splitter sizes from last session (after layout is
        # built so the splitter has children to size).
        QTimer.singleShot(0, self._restore_window_state)

    # ──────────────────────────────────────────────────────────────
    # teammate pane lifecycle
    # ──────────────────────────────────────────────────────────────
    def _ensure_teammate_pane(self, role_name: str, project: str) -> None:
        # Route to the tab that owns `project` so a spawn from a
        # background tab doesn't drop the new pane into whichever tab
        # is active right now (same multi-tab routing bug we fixed for
        # `paneClosed`). Falling back to the current tab when the
        # project's tab is missing keeps tests that drive the
        # orchestrator without a tab strip working.
        tab = self._tab_for_project(project) or self._current_tab()
        if role_name == LEAD.name or role_name in tab.teammate_panes:
            return
        role = by_name(role_name)
        if role is None:
            # custom role — use user-picked color if available, else default gray
            color = tab.custom_role_colors.get(role_name, "#94a3b8")
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
        tab.teammate_panes[role_name] = pane
        self.orch.register_pane(pane, project=tab.project_name)
        tab.teammate_split.addWidget(pane)

        # show right side and rebalance
        tab.show_teammate_split()
        tab.rebalance_teammates()
        self._status.showMessage(f"added pane · {role_name} ({tab.project_name})", 4_000)

    def _tab_for_project(self, project: str) -> ProjectTab | None:
        """Find the ProjectTab whose `project_name` matches. Returns None
        if no tab is open for that project (e.g. it was closed before
        the signal arrived)."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ProjectTab) and w.project_name == project:
                return w
        return None

    def _remove_teammate_pane(self, role_name: str, project: str) -> None:
        # Route the removal to the tab that owns `project`, not whichever
        # tab is active right now. Before this fix the slot popped from
        # `self.teammate_panes` (the *active* tab's dict), which left the
        # closed pane stranded in its real tab as a placeholder when the
        # user had switched tabs during the 2.5 s done-close window. That
        # was the "Backend empty slot" surfaced in the multi-tab regression.
        tab = self._tab_for_project(project)
        if tab is None:
            return
        pane = tab.teammate_panes.pop(role_name, None)
        if pane is None:
            return
        self.orch.unregister_pane(role_name, project=project)
        # Explicitly tear down the WebEngine view + its timers before
        # deleteLater. Without this Chromium's renderer process can linger
        # holding the scrollback heap until next GC, and a leftover
        # heartbeat timer can fire runJavaScript into a partially-destroyed
        # page on the way out.
        try:
            pane._terminal.destroy_terminal()
        except Exception:
            pass
        pane.setParent(None)
        pane.deleteLater()
        if not tab.teammate_panes:
            tab.hide_teammate_split()
        else:
            tab.rebalance_teammates()

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

        # Project may have been picked before boot — sync the rtk button to
        # match its current installation state.
        self._refresh_rtk_button()

        # Auto-run `/remote-control` in the Lead pane after spawn so the
        # user can drive the cockpit from a browser/phone without having to
        # remember to type it every session. Skip with
        # TAKKUB_AUTO_REMOTE_CONTROL=0 if the bridge is unwanted (e.g. CI).
        if ok and os.environ.get("TAKKUB_AUTO_REMOTE_CONTROL", "1") != "0":
            self.orch.inject_slash_command_when_ready(LEAD.name, "/remote-control")

        # Auto-spawn project presets after Lead has had a moment to boot.
        # Stagger 3s apart so we don't hammer the system or race on auto-trust.
        presets = preset_roles_for_active()
        if presets:
            self._status.showMessage(
                f"auto-spawning {len(presets)} preset role(s): {', '.join(presets)}",
                6_000,
            )
            for i, role in enumerate(presets):
                QTimer.singleShot(15_000 + i * 3_000, lambda r=role: self.orch.spawn(r))

        # Restore any extra tabs the user had open last session. The very
        # first tab is already in place (the active project), so we skip
        # it and reopen the rest with a small stagger so each Lead's
        # claude bootstrap doesn't collide.
        saved = get_open_tabs()
        already = set(self._open_projects())
        to_open = [n for n in saved if n not in already]
        if to_open:
            self._status.showMessage(
                f"restoring {len(to_open)} extra tab(s): {', '.join(to_open)}", 6_000
            )
            for i, name in enumerate(to_open):
                # 4s stagger so Leads don't all try to bind a renderer / read
                # claude binaries in parallel
                QTimer.singleShot(2_500 + i * 4_000, lambda n=name: self._open_project_tab(n))
        # Persist the current state (covers the case where saved tabs
        # referenced a now-deleted project and got dropped).
        self._persist_open_tabs()

        # Session resume: re-spawn teammate panes that were live at the
        # last shutdown. Wait until every Lead has had a chance to boot
        # (last tab open fires at `2_500 + (N-1) * 4_000`, plus ~10 s
        # for the trust-modal-auto-press and `/remote-control` injection
        # to settle) so teammate spawns don't race with their tab's Lead.
        n_extra = len(to_open)
        restore_delay = 2_500 + n_extra * 4_000 + 12_000
        QTimer.singleShot(restore_delay, self._restore_teammates_from_snapshot)

    def _restore_teammates_from_snapshot(self) -> None:
        """Read last-session.json and re-spawn the teammate panes that
        were recorded there. Lead is exempt (already restored via
        open_tabs). Status bar shows a hint so the user sees the
        restore happen."""
        scheduled = self.orch.restore_teammates()
        if scheduled:
            self._status.showMessage(
                f"restored {scheduled} teammate pane(s) from last session", 8_000
            )

    def _track_pane_request(self, role_name: str, project: str) -> None:
        # only called when a new pane is being created
        self._status.showMessage(f"opening pane · {role_name} ({project})", 3_000)

    def _notify_agent_done(self, role_name: str, note: str) -> None:
        """Show a desktop toast when an agent reports done."""
        title = f"{role_name} done"
        body = note.strip() if note else "task complete"
        # mirror to status bar too in case tray is unavailable
        self._status.showMessage(f"✓ {title}: {body[:80]}", 6_000)
        if self._tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 5_000)

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
        role, ok = QInputDialog.getItem(self, "Assign task", "Role:", choices, 0, False)
        if not ok or not role:
            return
        task, ok = QInputDialog.getMultiLineText(self, "Assign task", f"Task for {role}:", "")
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
            '<code>takkub assign --role &lt;name&gt; [--cwd &lt;path&gt;] "task"</code> — spawn + send task<br>'
            '<code>takkub send --to &lt;role&gt; "msg"</code> — peer message (Lead is CC\'d)<br>'
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

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F1:
            self._show_help()
            return
        super().keyPressEvent(event)

    def _update_status(self) -> None:
        from .token_meter import context_limit_for_model

        active = 0
        working = 0
        total_prompt = 0
        biggest_limit = 0
        per_role: list[tuple[str, int, int]] = []  # (role, prompt, limit)
        for p in self.orch.panes.values():
            if p.session is not None and p.session.is_alive:
                active += 1
                if p.state == "working":
                    working += 1
            usage = p.current_usage()
            if usage:
                limit = context_limit_for_model(usage["model"])
                total_prompt += usage["prompt"]
                biggest_limit = max(biggest_limit, limit)
                per_role.append((p.role.name, usage["prompt"], limit))

        # Refresh every tab's label so the user can see context pressure on
        # other projects without switching tabs. Tab label format:
        #   "<project>"        — no token data yet (or no panes)
        #   "<project> · 52k/200k"  — peak usage of any pane in the project
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if not isinstance(tab, ProjectTab):
                continue
            panes = self.orch._project_panes(tab.project_name)
            peak_ratio = 0.0
            peak_prompt = 0
            peak_limit = 0
            for role_name, pane in panes.items():
                usage = pane.current_usage()
                if not usage:
                    continue
                lim = context_limit_for_model(usage["model"])
                ratio = (usage["prompt"] / lim) if lim else 0.0
                if ratio > peak_ratio:
                    peak_ratio = ratio
                    peak_prompt = usage["prompt"]
                    peak_limit = lim
                # Surface a status-bar + tray warning the first time a
                # pane crosses 80%. Hysteresis at 70% keeps the toast
                # from flickering when usage oscillates near the line.
                key = f"{tab.project_name}::{role_name}"
                if ratio >= 0.80 and not self._context_warned.get(key):
                    self._context_warned[key] = True
                    msg = (
                        f"⚠ {tab.project_name}/{role_name} context at "
                        f"{int(ratio * 100)}% — consider /clear or ✅ Finish Job"
                    )
                    self._status.showMessage(msg, 12_000)
                    if self._tray and QSystemTrayIcon.isSystemTrayAvailable():
                        self._tray.showMessage(
                            f"Context {int(ratio * 100)}%",
                            f"{tab.project_name}/{role_name} — consider /clear or Finish Job",
                            QSystemTrayIcon.MessageIcon.Warning,
                            6_000,
                        )
                elif ratio < 0.70 and self._context_warned.get(key):
                    self._context_warned.pop(key, None)
            if peak_limit:
                self.tabs.setTabText(
                    i,
                    f"{tab.project_name} · "
                    f"{format_tokens(peak_prompt)}/{format_tokens(peak_limit)}",
                )
            else:
                self.tabs.setTabText(i, tab.project_name)
        port = self.cli._server.serverPort() if self.cli._server.isListening() else 0
        bits = [f"cockpit · cli {port}", f"{active} active"]
        if working:
            bits.append(f"{working} working")
        self._status.showMessage("  ·  ".join(bits))

        # Update aggregate token meter. The headline uses the *biggest* single
        # pane's limit (rather than summing limits) because each context is
        # independent — the team-wide ratio is "how close any pane is to its
        # cap" plus a sum for absolute reference.
        if per_role:
            ratio = max(p / lim for _, p, lim in per_role if lim) if per_role else 0.0
            color = usage_color(ratio)
            head = f"Σ {format_tokens(total_prompt)} · max {int(ratio * 100)}%"
            self._token_total.setText(head)
            self._token_total.setStyleSheet(
                f"color: {color}; font-size: 11px; padding: 0 6px; "
                "font-variant-numeric: tabular-nums;"
            )
            lines = [f"{r}: {format_tokens(pr)} / {format_tokens(lim)}" for r, pr, lim in per_role]
            self._token_total.setToolTip("Context occupancy per pane:\n" + "\n".join(lines))
            self._token_total.show()
        else:
            self._token_total.hide()

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
        # Kept for backwards compat with the (now-hidden) `_project_combo`.
        # The tab strip is the real switcher; nothing routes here anymore
        # unless a future caller programmatically pokes the combo.
        if not name:
            return
        if not set_active_project(name):
            return
        self._refresh_rtk_button()

    # ──────────────────────────────────────────────────────────────
    # multi-tab orchestration
    # ──────────────────────────────────────────────────────────────
    def _open_projects(self) -> list[str]:
        """Project names of every tab currently open. Excludes the "+"
        pseudo-tab automatically (its widget isn't a ProjectTab)."""
        return [
            self.tabs.widget(i).project_name
            for i in range(self.tabs.count())
            if isinstance(self.tabs.widget(i), ProjectTab)
        ]

    def _plus_tab_index(self) -> int:
        """Index of the trailing "+" pseudo-tab, or -1 if it isn't in
        the strip yet (e.g. during __init__ before it's added)."""
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is getattr(self, "_plus_tab_placeholder", None):
                return i
        return -1

    def _on_tab_bar_clicked(self, index: int) -> None:
        """Intercept clicks on the "+" pseudo-tab: prevent it from
        becoming the active tab and open the new-project picker instead.
        Other tab clicks fall through to QTabWidget's normal switch
        behavior (which triggers `currentChanged` → `_on_tab_switched`)."""
        if index == self._plus_tab_index():
            # Defer slightly so Qt's own click handling doesn't race the
            # `_on_new_tab_clicked` dialog (modal blocks until dismissed).
            QTimer.singleShot(0, self._on_new_tab_clicked)

    def _persist_open_tabs(self) -> None:
        """Snapshot the current tab order into projects.json so the next
        cockpit launch restores the same set of tabs in the same order.
        Best-effort; never raises so a config write blip can't take down
        the orchestrator."""
        try:
            set_open_tabs(self._open_projects())
        except Exception as e:
            self._status.showMessage(f"⚠ failed to persist open tabs: {e}", 8_000)

    def _on_new_tab_clicked(self) -> None:
        """Prompt for a project that isn't already open, then add it as a
        new tab and spawn Lead in it. The picker enforces the "one tab per
        project" rule by excluding open names."""
        from PyQt6.QtWidgets import QMessageBox

        open_names = set(self._open_projects())
        available = [n for n in list_project_names() if n not in open_names]
        if not available:
            QMessageBox.information(
                self,
                "Open tab",
                "Every configured project is already open. Use 📁 to add a new one.",
            )
            return
        name, ok = QInputDialog.getItem(
            self,
            "Open tab",
            "Pick a project to open in a new tab:",
            available,
            0,
            False,
        )
        if not ok or not name:
            return
        self._open_project_tab(name)

    def _open_project_tab(self, project_name: str) -> None:
        """Create a ProjectTab for `project_name`, register a fresh Lead
        pane in the orchestrator's per-project namespace, spawn the
        claude session, and auto-bridge to /remote-control. Becomes the
        focused tab on return.

        Uses the deferred-attach pattern (addTab BEFORE creating the
        Lead AgentPane) so QWebEngineView never gets re-parented after
        first paint — re-parenting a WebEngine-containing widget
        crashes Chromium's renderer on Windows.
        """
        # Set as active so spawn picks up lead_cwd() for the new project.
        set_active_project(project_name)
        self._refresh_project_list()

        tab = ProjectTab(project_name, lead_pane=None)
        # Always insert *before* the trailing "+" pseudo-tab so the "+"
        # stays anchored at the right edge of the strip.
        plus_idx = self._plus_tab_index()
        if plus_idx >= 0:
            idx = self.tabs.insertTab(plus_idx, tab, project_name)
        else:
            idx = self.tabs.addTab(tab, project_name)
        self.tabs.setCurrentIndex(idx)
        lead = AgentPane(LEAD, parent=tab)
        self.orch.register_pane(lead, project=project_name)
        tab.attach_lead(lead)

        ok, msg = self.orch.spawn(LEAD.name, project=project_name)
        if not ok:
            self._status.showMessage(f"⚠ Lead spawn failed for {project_name}: {msg}", 15_000)
            return
        lead._title.setText(f"Lead · {project_name}")
        if os.environ.get("TAKKUB_AUTO_REMOTE_CONTROL", "1") != "0":
            self.orch.inject_slash_command_when_ready(
                LEAD.name, "/remote-control", project=project_name
            )
        self._refresh_rtk_button()
        self._persist_open_tabs()
        self._status.showMessage(f"opened tab · {project_name}", 4_000)

    def _on_tab_close_requested(self, index: int) -> None:
        """Close the tab at `index`. Confirms first, then tears down every
        pane for that project (Lead + teammates). If the last tab closes,
        the cockpit shows the empty state — the user can `+` a new one."""
        from PyQt6.QtWidgets import QMessageBox

        tab = self.tabs.widget(index)
        if not isinstance(tab, ProjectTab):
            return
        confirm = QMessageBox.question(
            self,
            "Close tab",
            f"Close '{tab.project_name}' tab? Lead + every teammate pane for "
            f"this project will be terminated.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        self.orch.close_all_teammates(project=tab.project_name)
        self.orch.close(LEAD.name, project=tab.project_name)
        self.tabs.removeTab(index)
        # ProjectTab still holds references to AgentPane/TerminalWidget;
        # explicitly destroy them so Chromium releases the renderer.
        try:
            for pane in [tab.lead_pane, *tab.teammate_panes.values()]:
                pane._terminal.destroy_terminal()
        except Exception:
            pass
        tab.deleteLater()
        self._persist_open_tabs()
        self._status.showMessage(f"closed tab · {tab.project_name}", 4_000)

    def _on_tab_switched(self, index: int) -> None:
        """User clicked a different tab. Sync `active` in projects.json so
        the orchestrator's project-default resolution and the rtk button
        match the visible tab. The "+" pseudo-tab is intercepted in
        `_on_tab_bar_clicked` and never becomes the visible target, so we
        defensively skip it here too."""
        if index < 0:
            return
        tab = self.tabs.widget(index)
        if not isinstance(tab, ProjectTab):
            # "+" pseudo-tab landed here through some unusual path
            # (e.g. keyboard navigation). Snap back to the previous
            # real tab so the cockpit never shows an empty pane area.
            for i in range(self.tabs.count()):
                if isinstance(self.tabs.widget(i), ProjectTab):
                    self.tabs.setCurrentIndex(i)
                    break
            return
        if set_active_project(tab.project_name):
            self._refresh_rtk_button()
            self._status.showMessage(f"active project → {tab.project_name}", 3_000)

    def _restart_lead_for_active_project(self) -> None:
        """Close every pane and respawn Lead at the new project's lead_cwd().

        Used when the user switches the active project from the dropdown or
        adds a new one — the cockpit's Lead pane is anchored at the project's
        lead_cwd at spawn time, so it has to be killed and re-spawned for the
        new working directory to take effect. Teammate panes go too since
        they were scoped to the old project's paths.
        """
        # 1. Close teammates first (they depend on Lead's project for cwd).
        self.orch.close_all_teammates()
        # 2. Close Lead.
        self.orch.close(LEAD.name)
        # 3. Respawn Lead after a short delay so the PTY can fully release.
        #    Spawning immediately races the still-terminating session and the
        #    orchestrator would short-circuit with "already running".
        QTimer.singleShot(1_500, self._respawn_lead_post_restart)

    def _respawn_lead_post_restart(self) -> None:
        ok, msg = self.orch.spawn(LEAD.name)
        if not ok:
            self._status.showMessage(f"⚠ Lead respawn failed: {msg}", 15_000)
            return
        active = active_project()[0]
        if active:
            self.lead_pane._title.setText(f"Lead · {active}")
        # Auto-bridge to the browser again on the fresh session.
        if os.environ.get("TAKKUB_AUTO_REMOTE_CONTROL", "1") != "0":
            self.orch.inject_slash_command_when_ready(LEAD.name, "/remote-control")

    def _refresh_rtk_button(self) -> None:
        """Show the install button only when the active project's lead_cwd()
        doesn't already carry the rtk hook. Hidden when rtk isn't on PATH or
        no project is active."""
        import time as _t
        from pathlib import Path as _P

        bin_ok = rtk_binary_available()
        root = lead_cwd()
        installed = is_rtk_installed(root) if root else None

        if not bin_ok:
            self._btn_install_rtk.hide()
            decision = "hide:no-rtk-binary"
        elif not root:
            self._btn_install_rtk.hide()
            decision = "hide:no-lead-cwd"
        elif installed:
            self._btn_install_rtk.hide()
            decision = "hide:already-installed"
        else:
            self._btn_install_rtk.show()
            self._btn_install_rtk.raise_()
            decision = "show:not-installed"

        # Diagnostic breadcrumb. Written to runtime/rtk_button.log so we
        # can confirm whether the cockpit's pythonw actually executed this
        # path (vs. running a stale cached process / older code). Remove
        # after visibility is verified.
        try:
            log = _P(__file__).resolve().parents[2] / "runtime" / "rtk_button.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            visible = self._btn_install_rtk.isVisible()
            with log.open("a", encoding="utf-8") as f:
                f.write(
                    f"{_t.strftime('%H:%M:%S')} bin_ok={bin_ok} "
                    f"root={root!r} installed={installed} "
                    f"decision={decision} isVisible={visible}\n"
                )
        except Exception:
            pass

    def _on_finish_job_clicked(self) -> None:
        """User-driven job wrap-up. Asks Lead for a closing summary,
        appends today's per-project session digest to the vault's
        05-Daily note, then closes every teammate. Lead stays alive
        so the user can read the summary and queue the next task
        without restarting the cockpit.

        PMS task auto-creation used to be an optional second step
        here; it was removed at the user's request so Finish Job
        stays a single-click action without a list-id prompt. PMS
        tasks are created manually by Lead when the user asks.
        """
        from PyQt6.QtWidgets import QMessageBox

        lead = self.orch.panes.get(LEAD.name)
        if lead is None or lead.session is None or not lead.session.is_alive:
            QMessageBox.warning(
                self,
                "Finish Job",
                "Lead pane isn't running. Spawn it first, then try again.",
            )
            return

        teammates = [n for n in self.orch.panes if n != LEAD.name and self.orch.panes[n].session]
        teammate_line = ", ".join(teammates) if teammates else "(none)"

        confirm = QMessageBox.question(
            self,
            "Finish Job",
            (
                "Wrap up the current session?\n\n"
                "Lead will be asked to write a closing summary covering:\n"
                "  • what was accomplished\n"
                "  • what's blocked or unfinished\n"
                "  • recommended next steps\n\n"
                f"After the summary, these teammate panes will be closed:\n"
                f"  {teammate_line}\n\n"
                "Today's session digest is also appended to "
                "<vault>/05-Daily/<date>.md.\n\n"
                "Lead stays running so you can read the summary."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        # Compose the wrap-up prompt. Kept Thai-leaning because the rest of
        # the cockpit's Lead instructions are Thai, but English fallback
        # phrases ensure non-Thai users still parse it.
        summary_prompt = (
            "[FINISH JOB] User marked the session as done. "
            "เขียน final summary แบบ structured ก่อนปิดงาน:\n\n"
            "## ✅ Accomplished\n"
            "- (สิ่งที่ทำเสร็จใน session นี้, 1 บรรทัดต่อรายการ)\n\n"
            "## ⚠️ Blockers / Unfinished\n"
            "- (สิ่งที่ติด หรือยังไม่เสร็จ)\n\n"
            "## ➡️ Next Steps\n"
            "- (เริ่มจากตรงไหนต่อ session ถัดไป)\n\n"
            "หลังจากเขียน summary เสร็จ run `takkub close-all` "
            "เพื่อปิด teammate panes ที่เหลือ"
        )
        # Inject as a regular task so Lead acknowledges the working state
        # while it writes the summary. close-all happens via Lead itself
        # (it has the gate permission); we also defensively close from the
        # cockpit side after a delay in case Lead drifted. 60 s is enough
        # for a plain summary write with no MCP work involved.
        self.orch._send_when_ready(LEAD.name, summary_prompt)
        QTimer.singleShot(60_000, lambda: self.orch.close_all_teammates())

        # Append today's session digest for this project to the vault's
        # 05-Daily note. Best-effort: silent when no vault is set up,
        # but the status bar surfaces the outcome so the user knows the
        # daily note was (or wasn't) touched.
        active = active_project()[0]
        digest_ok = bool(active and self.orch.write_daily_digest(active))
        digest_suffix = " · daily digest appended" if digest_ok else ""

        self._status.showMessage(
            f"Finish Job: Lead is writing the summary{digest_suffix}. "
            "Teammates will close shortly.",
            8_000,
        )

    def _on_install_rtk_clicked(self) -> None:
        from PyQt6.QtWidgets import QMessageBox

        root = lead_cwd()
        if not root:
            QMessageBox.warning(
                self,
                "rtk install",
                "No active project — pick one in the dropdown first.",
            )
            return

        proj_name = active_project()[0] or "(unnamed)"
        confirm = QMessageBox.question(
            self,
            "Install rtk hook",
            (
                f"Add the rtk PreToolUse Bash hook to:\n\n"
                f"  {root}/.claude/settings.json\n\n"
                f"Every Bash tool call in panes under this project ({proj_name}) "
                f"will be auto-rewritten with rtk (60-90% token savings on common "
                f"dev output like git diff, docker logs, npm ci, pytest, tsc).\n\n"
                f"This only touches the project's .claude/settings.json — no user-level "
                f"settings, no CLAUDE.md changes."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        ok, msg = install_rtk(root)
        if ok:
            self._status.showMessage(f"rtk: {msg}", 6_000)
        else:
            QMessageBox.critical(self, "rtk install failed", msg)
        self._refresh_rtk_button()

    def _on_add_project_clicked(self) -> None:
        import json
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )

        from .config import PROJECTS_JSON, load_projects

        dir_path = QFileDialog.getExistingDirectory(self, "Select Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = p.name

        # Create a custom dialog to let the user map paths
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Configure Project Paths: {name}")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Map subdirectories to role keys (e.g., 'web', 'api').\nLeave blank to ignore a directory."
            )
        )

        form = QFormLayout()
        layout.addLayout(form)

        # Check if we already have this project configured
        data = load_projects()
        existing_paths = {}
        existing_paths_rev = {}
        if "projects" in data and name in data["projects"]:
            existing_paths = data["projects"][name].get("paths", {})
            # Reverse mapping: value (path) -> key (role)
            existing_paths_rev = {v: k for k, v in existing_paths.items()}

        inputs = {}
        for sub in p.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                le = QLineEdit()
                le.setPlaceholderText("key (e.g. web, api)")

                # Pre-fill if we already saved this path previously
                sub_posix = str(sub.resolve().as_posix())
                if sub_posix in existing_paths_rev:
                    le.setText(existing_paths_rev[sub_posix])

                form.addRow(sub.name, le)
                inputs[sub.name] = (sub, le)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        paths = {}
        for _sub_name, (sub_path, le) in inputs.items():
            key = le.text().strip()
            if key:
                paths[key] = str(sub_path.resolve().as_posix())

        if not paths:
            # Fallback if they mapped nothing
            paths["main"] = str(p.resolve().as_posix())

        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}

        data["projects"][name] = {"description": name, "paths": paths, "presets": []}
        data["active"] = name

        PROJECTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        self._refresh_project_list()
        # If this project already has a tab (re-adding the same name) just
        # focus it; otherwise open it as a fresh tab beside the current
        # ones. Either way the user lands on the new project without
        # disturbing other open tabs.
        if name in self._open_projects():
            for i in range(self.tabs.count()):
                if (
                    isinstance(self.tabs.widget(i), ProjectTab)
                    and self.tabs.widget(i).project_name == name
                ):
                    self.tabs.setCurrentIndex(i)
                    break
            self._status.showMessage(f"Updated project: {name}", 4_000)
        else:
            self._status.showMessage(f"Added project: {name} (opening tab...)", 4_000)
            self._open_project_tab(name)

    # ──────────────────────────────────────────────────────────────
    # + pane button: spawn an additional teammate from a role name
    # ──────────────────────────────────────────────────────────────
    def _on_add_pane_clicked(self) -> None:
        # roles already shown (skip them from the picker)
        taken = set(self.orch.panes.keys())
        default_unused = [r.name for r in DEFAULT_TEAMMATES if r.name not in taken]
        choices = [*default_unused, "custom..."]
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
    def closeEvent(self, event) -> None:
        from PyQt6.QtWidgets import QMessageBox

        # Multi-tab guard: closing the cockpit with multiple project tabs
        # tears down *every* Lead + teammate session in one shot, which is
        # easy to do by accident (Alt+F4 muscle memory). Single tab gets
        # the usual silent close.
        open_names = self._open_projects()
        if len(open_names) > 1:
            confirm = QMessageBox.question(
                self,
                "Close cockpit",
                (
                    f"Close cockpit with {len(open_names)} tabs open?\n\n"
                    f"Lead + every teammate pane in these projects will end:\n"
                    f"  {', '.join(open_names)}"
                ),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Ok:
                event.ignore()
                return

        self._save_window_state()
        self._persist_open_tabs()
        # Capture the live teammate-pane picture BEFORE we terminate the
        # sessions below — otherwise everything would look dead by the
        # time the snapshot serialiser runs. The next cockpit launch
        # reads this file to re-spawn teammates with `--continue`.
        self.orch.write_session_snapshot()
        # Also write a per-project resume brief (~last 20 exchanges)
        # to `<vault>/07-AI-Command-Center/briefs/` so the next session
        # can read it and pick up context without scrolling pane
        # history. Best-effort: no vault → silently skipped.
        try:
            self.orch.write_resume_briefs()
        except Exception:
            pass
        # Walk every project namespace, not just the active view, so a
        # background tab's panes are also terminated cleanly.
        for project_panes in self.orch._panes_by_project.values():
            for pane in list(project_panes.values()):
                if pane.session is not None:
                    pane.mark_expected_exit()
                    pane.session.terminate()
        self.cli.close()
        super().closeEvent(event)


# DEFAULT_TEAMMATES is imported only so the symbol stays referenced (for the
# CLI tab-completion / future role picker UI). Suppress unused warning.
_ = DEFAULT_TEAMMATES
