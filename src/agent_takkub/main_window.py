"""Main cockpit window.

Layout (2026-06-26 redesign — presentation only):
  - A left **sidebar** (`ProjectNav`) lists every open project; the selected
    project's `ProjectTab` fills the right.
  - Inside a ProjectTab every pane — Lead + each teammate — is a **tab**, so
    one full-size pane shows at a time and the user switches with the tab strip.
  - When Lead does `takkub assign --role X`, the orchestrator emits
    `paneRequested("X")`; MainWindow creates an AgentPane and adds it as a new
    pane-tab. `takkub done` / the pane × removes that tab.
  - Only the visible pane of the visible project paints (keep-alive); the rest
    suspend so Chromium can release their renderer RAM. A red dot lands on the
    Lead pane-tab when a notice arrives while the user is on another pane.

`ProjectNav` exposes a QTabWidget-compatible API so MainWindow's project call
sites (widget/count/addTab/removeTab/setCurrentIndex/currentChanged) are
unchanged from the old top-tab-strip implementation.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QStyle,
    QSystemTrayIcon,
    QWidget,
)

from . import cockpit_theme
from .agent_pane import AgentPane
from .cli_server import CliServer
from .config import (
    EVENTS_LOG,
    active_project,
    clear_active_project,
    get_open_tabs,
    list_project_names,
    preset_roles_for_active,
    project_folder_exists,
    set_active_project,
    set_open_tabs,
)
from .limit_panel import LimitPanelMixin
from .logs_panel import LogsPanel
from .orchestrator import Orchestrator, _log_event
from .project_nav import ProjectNav
from .project_tab import ProjectTab
from .project_wizard import ProjectWizardMixin
from .roles import DEFAULT_TEAMMATES, LEAD, Role, by_name
from .status_header import StatusHeaderMixin
from .task_dock import COLLAPSED_W as _TASKS_DOCK_COLLAPSED_W
from .task_dock import EXPANDED_MIN_W as _TASKS_DOCK_EXPANDED_W
from .task_dock import TaskDockWidget
from .tutorial_overlay import TutorialOverlay, TutorialStep, has_seen_tutorial
from .update_panel import MainWindowUpdateMixin
from .usage_meter import UsageMeter
from .user_actions import UserActionsMixin

# Tier 1 quiet-boot constants: event-driven debounce before Lead spawn.
# Each poll fires from a separate QTimer callback (one event-loop turn).
# Streak resets when InSendMessageEx / modal-popup / app-inactive observed.
_BOOT_LEAD_INITIAL_MS = 150  # wait after first-paint before starting debounce
_BOOT_LEAD_POLL_MS = 50  # ms between debounce turns
_BOOT_LEAD_QUIET_N = 3  # consecutive clear turns required (~150-250 ms total)


def _handle_cli_bind_error(error_msg: str) -> None:
    """Called when CliServer.listen() fails. Logs, shows a critical dialog,
    then exits the application. The cockpit cannot function without the CLI
    server — pane communication (takkub send/done/list) is fully broken.
    """
    # 1. Persist to events.log so post-mortem debugging can confirm the cause.
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "cli_bind_failed",
                "error": error_msg,
            },
            ensure_ascii=False,
        )
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass

    # 2. Show actionable dialog so the user knows exactly what happened.
    QMessageBox.critical(
        None,
        "CLI server failed to start",
        f"The cockpit's internal CLI server could not bind to a port:\n\n"
        f"  {error_msg}\n\n"
        "Most likely causes:\n"
        "  • Another cockpit instance is already running\n"
        "  • Antivirus or firewall is blocking loopback sockets\n"
        "  • Windows socket layer issue (try restarting the machine)\n\n"
        "To find what is holding the port:\n"
        "  netstat -ano | findstr :LISTENING\n\n"
        "The cockpit will now exit. Close any other cockpit windows and retry.",
    )

    # 3. Graceful exit — pane workflow is non-functional without the CLI.
    QApplication.quit()


class MainWindow(
    MainWindowUpdateMixin,
    ProjectWizardMixin,
    UserActionsMixin,
    LimitPanelMixin,
    StatusHeaderMixin,
    QMainWindow,
):
    # Thread-safe signal: background limit_status.Poller emits UsageData|None
    # from a daemon thread → Qt queues the call so _on_usage_updated runs on the GUI thread.
    _usageUpdated: pyqtSignal = pyqtSignal(object)

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
    def _custom_role_colors(self) -> dict[str, str]:
        return self._current_tab().custom_role_colors

    def __init__(self) -> None:
        super().__init__()
        from .config import instance_window_title

        self.setWindowTitle(f"{instance_window_title()} — dev team cockpit")

        # Shipped inside the package (wheel) as static/icon.png so the taskbar /
        # title-bar icon works from an `npm install`/pip install; fall back to
        # the repo-root assets/ for an editable dev checkout.
        icon_path = Path(__file__).parent / "static" / "icon.png"
        if not icon_path.exists():
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
            f"QMainWindow {{ background-color: {cockpit_theme.GROUND_BODY}; }}"
            f"QStatusBar {{ background: {cockpit_theme.GROUND_PANEL}; "
            f"color: {cockpit_theme.TEXT_MUTED}; }}"
            f"QSplitter::handle {{ background: {cockpit_theme.GROUND_SELECT}; }}"
            f"QSplitter::handle:hover {{ background: {cockpit_theme.BORDER_STRONG2}; }}"
        )

        # ── orchestrator + cli server ───────────────────────────
        self.orch = Orchestrator(self)
        # Inject the 3-layer spawn gate predicate (layers 1+2; layer 3 is the
        # Win32 InSendMessageEx check inside spawn_gate.py itself).
        # Prevents RPC_E_CANTCALLOUT_ININPUTSYNCCALL when QTimer fires during
        # a modal dialog, QMenu, or other nested Qt event loop.
        from PyQt6.QtWidgets import QApplication as _QApp

        self.orch.set_spawn_guard(
            lambda: _QApp.activeModalWidget() is not None or _QApp.activePopupWidget() is not None
        )
        self.cli = CliServer(self.orch, self)
        self.orch.paneRequested.connect(self._ensure_teammate_pane)
        self.orch.paneClosed.connect(self._remove_teammate_pane)
        self.orch.agentDone.connect(self._notify_agent_done)
        self.orch.crossTabDone.connect(self._on_cross_tab_done)
        self.orch.leadNotified.connect(self._on_lead_notified)
        # `takkub restart` — same persist+relaunch path as the status-bar 🔄
        # button, minus the confirm dialog (typing the command IS the confirm).
        self.orch.restartRequested.connect(self._restart_cockpit)

        # ── system tray for desktop notifications ───────────────
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(app_icon)
        self._tray.setToolTip("agent-takkub")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

        # ── root layout ─────────────────────────────────────────
        root = QWidget(self)
        outer = QHBoxLayout(root)
        # Edge-to-edge app-shell: the sidebar sits flush against the window
        # frame (modern look); ProjectNav paints its own panels/borders.
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        # Central navigator: a left sidebar lists open projects, the right side
        # stacks each project's ProjectTab (whose panes are themselves tabs).
        # ProjectNav exposes a QTabWidget-compatible API (widget/count/addTab/
        # removeTab/setCurrentIndex/currentChanged) so the rest of MainWindow is
        # unchanged; tab-bar-only concerns surface as dedicated signals.
        self.tabs = ProjectNav(self)
        self.tabs.addRequested.connect(self._on_new_tab_clicked)
        self.tabs.closeRequested.connect(self._on_tab_close_requested)
        self.tabs.contextMenuRequested.connect(self._on_tab_context_menu)
        self.tabs.openProjectRequested.connect(self._open_project_tab)
        # NOTE: `currentChanged` is connected at the end of __init__ — once the
        # status-bar widgets exist. It fires the moment the first row is added,
        # and the slot calls `_refresh_rtk_button` which touches
        # `self._btn_install_rtk`. Connecting up-front would fire the slot before
        # that button exists, raising AttributeError inside a Qt slot — which the
        # event-dispatch path then surfaces as a silent Chromium renderer crash
        # on Windows (pythonw shows nothing; the cockpit window never appears).

        outer.addWidget(self.tabs, 1)

        # Build the initial tab for the active project. Order matters: adding the
        # ProjectTab to the stack re-parents it; if a QWebEngineView were already
        # nested inside when that happens, Chromium's renderer crashes silently
        # on Windows ("composition surface invalidated"). Adding the tab BEFORE
        # creating the Lead AgentPane keeps the chain stable from first paint on.
        initial_project = active_project()[0] or "default"
        initial_tab = ProjectTab(initial_project, lead_pane=None)
        self.tabs.addTab(initial_tab, initial_project)
        self._wire_project_tab(initial_tab)
        initial_lead = AgentPane(LEAD, parent=initial_tab)
        self.orch.register_pane(initial_lead, project=initial_project)
        initial_tab.attach_lead(initial_lead)
        self.tabs.setCurrentIndex(0)

        # Usage-window readout — sits as the corner widget of the active
        # project's pane_tabs (top-right, same row as the Lead/teammate tabs).
        # On every project switch _on_tab_switched reparents this single label
        # into the new active ProjectTab's corner via mount_usage_widget().
        self._limit_label = UsageMeter()
        self._limit_label.setToolTip(
            "Claude usage windows (5h / 7d)\n"
            "Reflects the User: profile selected for this project.\n"
            "Updates every 5 min."
        )

        # ── status bar ────────────────────────────────────
        self._build_status_bar()
        # Mount meter into the initial tab's pane_tabs corner.
        # _limit_label_host tracks the current owner so _on_tab_switched can
        # explicitly clear the old corner before mounting on the new tab.
        self._limit_label_host: ProjectTab | None = initial_tab
        initial_tab.mount_usage_widget(self._limit_label)

        # Only NOW is it safe to listen for project switches — the handler
        # touches `_btn_install_rtk` via `_refresh_rtk_button`, which didn't
        # exist when the first row was added (see the deferred-connect note).
        self.tabs.currentChanged.connect(self._on_tab_switched)

        # ── limit-status store (background thread → GUI via signal) ───
        self._limit_store = None
        self._usageUpdated.connect(self._on_usage_updated)
        # Defer 3 s so the boot sequence settles before the first HTTP hit.
        QTimer.singleShot(3_000, self._init_limit_store)

        # First-run guided tour — fire once the window is up and the status-bar
        # chips it points at exist. Persisted flag makes it once-per-install;
        # replayable from the ❓ Tour button.
        QTimer.singleShot(1_500, self._maybe_autostart_tutorial)

        # ── bottom logs dock (hidden by default) ────────────────
        self._logs_dock = QDockWidget("events", self)
        self._logs_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._logs_dock.setWidget(LogsPanel(EVENTS_LOG))
        self._logs_dock.setMinimumHeight(140)
        self._logs_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._logs_dock)

        # ── right-side task-tree dock (A8, hidden by default) ───
        self._tasks_dock = QDockWidget("Task List", self)
        self._tasks_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self._configure_tasks_dock_chrome(self._tasks_dock)
        self._tasks_dock_widget = TaskDockWidget()
        self._tasks_dock.setWidget(self._tasks_dock_widget)
        self._tasks_dock.setMinimumWidth(_TASKS_DOCK_EXPANDED_W)
        self._tasks_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._tasks_dock)
        # Belt-and-suspenders: DockWidgetFloatable is intentionally never set
        # (see _configure_tasks_dock_chrome), so the user can't drag it into
        # a floating top-level window — but if anything ever flips
        # isFloating() anyway (e.g. a future features change), snap it back
        # instead of letting it render with OS window chrome.
        self._tasks_dock.topLevelChanged.connect(
            lambda floating: self._tasks_dock.setFloating(False) if floating else None
        )
        self._tasks_dock_anim: QParallelAnimationGroup | None = None
        self._tasks_dock_widget.collapseToggled.connect(self._on_tasks_dock_collapse_toggled)
        # Live refresh: the Task Ledger (A7) writes on every assign/done/
        # fail/close; Orchestrator emits ledgerChanged right after each write
        # lands, so the dock repaints only the one project card that changed
        # instead of polling the state file on a timer.
        self.orch.ledgerChanged.connect(self._tasks_dock_widget.refresh_project)

        # Periodic session snapshot so a hard crash (Alt+F4 mishandled,
        # power loss, force-kill from Task Manager) still leaves a recent
        # picture on disk for the next launch. closeEvent writes one final
        # time on a clean shutdown.
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setInterval(60_000)
        self._session_save_timer.timeout.connect(self.orch.write_session_snapshot)
        self._session_save_timer.start()

        # Heartbeat for the dead-man watchdog in app.py._start_deadman_watchdog.
        # The watchdog daemon thread reads _heartbeat_ts; if it stops advancing
        # for ~30 s the watchdog assumes the main thread is wedged and calls
        # os._exit(1). Writing a float is effectively atomic under CPython's GIL.
        #
        # Interval is 250 ms (not 1 s) so the watchdog can resolve SUB-SECOND
        # main-thread stalls — the brief freezes felt while typing during a pane
        # spawn. With a 1 s beat, normal age regularly approached 1 s and would
        # false-trigger the 0.75 s soft-stall log threshold; a 250 ms beat keeps
        # normal age well under it. The extra timestamp writes are negligible.
        self._heartbeat_ts: float = time.monotonic()
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(250)
        self._heartbeat_timer.timeout.connect(self._tick_heartbeat)
        self._heartbeat_timer.start()

        # Update check — two-stage strategy:
        # 1. singleShot 30 s after boot: first poll without blocking startup.
        # 2. Recurring 5-minute timer started *after* the singleShot fires
        #    (wired inside _schedule_update_check) so fetch never runs on
        #    the Qt main thread — each poll offloads to a QThreadPool worker.
        QTimer.singleShot(30_000, self._schedule_update_check)
        self._update_poll_timer = QTimer(self)
        self._update_poll_timer.setInterval(5 * 60 * 1000)  # 5 minutes
        self._update_poll_timer.timeout.connect(self._schedule_update_check)
        # Start after the singleShot so the first background fetch doesn't
        # race with the boot fetch.
        QTimer.singleShot(30_000, self._update_poll_timer.start)

        self._install_shortcuts()

        # ── boot: start CLI server + auto-spawn Lead ────────────
        QTimer.singleShot(0, self._boot)

        # Restore window/splitter sizes from last session (after layout is
        # built so the splitter has children to size).
        QTimer.singleShot(0, self._restore_window_state)

    # ──────────────────────────────────────────────────────────────
    # teammate pane lifecycle
    # ──────────────────────────────────────────────────────────────
    def _wire_project_tab(self, tab: ProjectTab) -> None:
        """Route a ProjectTab's pane-tab × close button into the orchestrator
        close chain — the same teardown path the pane header's own × uses."""
        tab.paneCloseRequested.connect(
            lambda role, proj=tab.project_name: self.orch.close(role, project=proj)
        )

    def _ensure_teammate_pane(self, role_name: str, project: str) -> None:
        if role_name == LEAD.name:
            return
        # Route to the tab that OWNS `project`. The old code fell back to the
        # focused tab when `_tab_for_project` missed — but that registers the
        # pane under the wrong project, so orchestrator.spawn() (checking the
        # right project) can't find it and silently drops the assign (#26 root
        # cause). Only borrow the current tab when it genuinely serves this
        # project (covers unit tests that drive one tab); otherwise bail and
        # let spawn() fail loudly + warn the Lead.
        tab = self._tab_for_project(project)
        if tab is None:
            cur = self._current_tab()
            resolved = self.orch._resolve_project(project)
            if isinstance(cur, ProjectTab) and cur.project_name == resolved:
                tab = cur
            else:
                return
        existing = tab.teammate_panes.get(role_name)
        if existing is not None:
            # Repair a registry desync: a pane lingering in the tab dict but
            # missing from the orchestrator registry (e.g. a stale entry from a
            # wrong-tab close) would make spawn() report the role as missing.
            # Re-register so both views agree instead of early-returning into
            # the silent-drop path.
            if self.orch._project_panes(tab.project_name).get(role_name) is not existing:
                self.orch.register_pane(existing, project=tab.project_name)
            return
        # For shard panes like "qa#1", look up the base role ("qa") so we get
        # the correct color and grid position; but create the Role with the full
        # pane_key as name so register_pane stores it under the right key.
        from .orchestrator import _split_shard as _mw_split_shard

        base_role_mw, shard_idx_mw = _mw_split_shard(role_name)
        role = by_name(base_role_mw) if shard_idx_mw is not None else by_name(role_name)
        if role is None:
            # custom role — use user-picked color if available, else default gray
            color = tab.custom_role_colors.get(role_name, cockpit_theme.ROLE_COLOR_FALLBACK)
            role = Role(
                name=role_name,
                label=role_name.capitalize(),
                color=color,
                column=2,
                row=99,
            )
        elif shard_idx_mw is not None:
            # Shard: keep base role's color/position but give it the pane_key
            # as name so the registry stores it under "qa#1", not "qa".
            role = Role(
                name=role_name,
                label=f"{role.label}#{shard_idx_mw}",
                color=role.color,
                column=role.column,
                row=role.row,
            )

        pane = AgentPane(role)
        # close routing: AgentPane.closeRequested (pane header ×) AND the
        # pane-tab × both reach orchestrator.close → paneClosed →
        # _remove_teammate_pane. One teardown path, no race.
        self.orch.register_pane(pane, project=tab.project_name)
        # add_teammate_tab registers the pane, adds its tab, and (via
        # _apply_pane_keepalive) leaves it suspended unless it's the visible
        # pane of the visible project — so a teammate spawned into a background
        # project, or a non-focused pane-tab, doesn't paint or leak RAM.
        tab.add_teammate_tab(role_name, pane, role.label)
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

        # Defer the ENTIRE removal (tab removal + WebEngine teardown) to a fresh
        # event-loop tick. This slot runs *synchronously* inside
        # Orchestrator.close()'s `paneClosed.emit()`. Mutating this pane's
        # QWebEngineView — `remove_teammate_tab`'s `removeTab` reparents it, and
        # `destroy_terminal` deletes it — reentrantly, nested in that emission,
        # tripped Qt6Core's __fastfail
        # (0xc0000409) and hard-crashed the cockpit on every pane close (done
        # auto-close, close-all, tab switch). The Qt 6.8 downgrade did NOT fix it
        # because the fault is this reentrancy, not a Qt regression. The earlier
        # partial fix deferred only destroy_terminal but left `removeTab` (which
        # also reparents the live view) inside the emit, so the crash recurred.
        # Running the whole teardown on the next tick lets the emit fully unwind
        # first, so no WebEngine operation ever overlaps another.
        def _teardown() -> None:
            pane = tab.teammate_panes.get(role_name)
            if pane is None:
                return
            # destroy_terminal FIRST: it removeEventFilter()s the view (so the
            # later removeTab/setParent can't re-enter eventFilter on it), stops
            # the heartbeat/flush timers (no stray runJavaScript into a dead
            # page), and deleteLater()s the page+view so Chromium releases the
            # renderer + scrollback heap promptly instead of at next GC.
            try:
                pane._terminal.destroy_terminal()
            except Exception:
                pass
            tab.remove_teammate_tab(role_name)
            self.orch.unregister_pane(role_name, project=project)
            pane.setParent(None)
            pane.deleteLater()

        QTimer.singleShot(0, _teardown)

    # ──────────────────────────────────────────────────────────────
    # first-run guided tour
    # ──────────────────────────────────────────────────────────────
    def _build_tutorial_steps(self) -> list[TutorialStep]:
        """Steps for the guided tour — "how to start", in order."""

        def _lead_pane():
            tab = self._current_tab()
            return getattr(tab, "lead_pane", None) if tab is not None else None

        return [
            TutorialStep(
                lambda: getattr(self.tabs, "_add_btn", None),
                "1 · เพิ่มโปรเจค",
                "เริ่มที่นี่ — กด “+ New project” เพื่อเปิดหรือ import โปรเจคที่อยากให้ทีมช่วยทำ "
                "แต่ละโปรเจคมี Lead + teammates แยกกัน",
            ),
            TutorialStep(
                _lead_pane,
                "2 · คุยกับ Lead",
                "พิมพ์บอก Lead ตรงนี้ว่าอยากทำอะไร (เช่น “เพิ่มหน้า login”) แล้วกด Enter — "
                "Lead จะวางแผนแล้ว spawn teammate ที่เหมาะกับงานให้เอง",
            ),
            TutorialStep(
                lambda: getattr(self, "_chip_exec_mode", None),
                "3 · โหมดทำงาน",
                "สลับ 1:1 (ทีละงาน) ↔ Multi (แตกงานอิสระให้หลาย agent ทำขนานกัน) — "
                "งานใหญ่หลายส่วนเปิด Multi แล้วจบไวขึ้น",
            ),
            TutorialStep(
                lambda: getattr(self, "_btn_pipelines", None),
                "4 · Team",
                "ตั้งค่าว่าแต่ละ role จะได้ MCP / plugin อะไรบ้าง (เช่น browser automation ให้ QA) "
                "หรือสร้าง role ใหม่ — มีผลกับ pane ที่ spawn ใหม่ทันที",
            ),
            TutorialStep(
                lambda: getattr(self, "_btn_doctor", None),
                "5 · Doctor",
                "เช็คว่าเครื่องพร้อม — เวอร์ชัน core, plugins, MCPs, providers ครบไหม "
                "กด Fix ซ่อมอัตโนมัติได้เลย",
            ),
            TutorialStep(
                lambda: getattr(self, "_btn_end_session", None),
                "6 · จบงาน",
                "พอเสร็จกด End Session — เขียนสรุปสั้นๆ ปิด teammate ทั้งหมด แล้วบันทึกไว้ "
                "session หน้าเปิดมา Lead จะจำได้ว่าทำอะไรค้างไว้",
            ),
        ]

    def _start_tutorial(self) -> None:
        """Show the guided-tour overlay (from the ❓ Tour button or first run)."""
        existing = getattr(self, "_tutorial", None)
        if existing is not None:
            try:
                existing.finish(mark=False)
            except RuntimeError:
                pass
        self._tutorial = TutorialOverlay(self, self._build_tutorial_steps())
        self._tutorial.start()

    def _maybe_autostart_tutorial(self) -> None:
        """Open the tour once per install (first launch), then never again unless
        replayed from the ❓ Tour button."""
        if not has_seen_tutorial():
            self._start_tutorial()

    # ──────────────────────────────────────────────────────────────
    def _boot(self) -> None:
        try:
            port = self.cli.listen()
            self._status.showMessage(f"cli port {port} — waiting for quiet boot...")
        except Exception as e:
            _handle_cli_bind_error(str(e))
            return  # QApplication.quit() is pending; don't proceed

        # Remote-control bolt-on (P0 scaffold, off-by-default). Dynamic import
        # so deleting src/agent_takkub/remote/ is just a ModuleNotFoundError
        # no-op and import-linter never sees a static core->remote edge. See
        # remote-control-plan/2026-07-07-remote-control.md §4/§13 (B4/C1/C2/C6).
        try:
            import importlib

            _remote_mod = importlib.import_module("agent_takkub.remote")
            self._remote = _remote_mod.RemoteControl.maybe_start(self.orch)
        except ModuleNotFoundError:  # folder deleted = uninstall no-op (B4)
            self._remote = None
        except Exception:  # any other error: never leave a half-open socket
            self._remote = None
            _log_event("remote_boot_failed")
        self._refresh_remote_chip()

        # Reflect orchestrator errors (eg. claude.exe not found) into the
        # status bar instead of swallowing them silently.
        self.orch.paneRequested.connect(self._track_pane_request)

        # Surface label "Lead · <project>" immediately (doesn't need spawn).
        active = active_project()[0]
        if active:
            self.lead_pane._title.setText(f"Lead · {active}")
            # If the active project's folder was deleted on disk, Lead can't
            # spawn there. _spawn_lead_when_quiet would fail gracefully (no
            # freeze, since _pty_backend raises now), but warn up-front so the
            # empty Lead pane isn't a mystery.
            if not project_folder_exists(active):
                self._status.showMessage(
                    f"⚠ active project '{active}' folder is missing on disk — "
                    f"Lead can't start. Restore the folder or pick another project.",
                    0,
                )

        # Project may have been picked before boot — sync the rtk button to
        # match its current installation state.
        self._refresh_rtk_button()

        # Tier 1: defer Lead spawn until the activation storm settles.
        # singleShot(0) fires during window-activation SendMessage processing
        # which triggers 0x8001010d.  Wait _BOOT_LEAD_INITIAL_MS then poll
        # until InSendMessageEx + modal/popup gate stays clear for
        # _BOOT_LEAD_QUIET_N consecutive event-loop turns.
        self._boot_quiet_count = 0
        QTimer.singleShot(_BOOT_LEAD_INITIAL_MS, self._spawn_lead_when_quiet)

        # (The /remote-control auto-bridge was removed 2026-07-10 — it raced
        # claude's /resume picker. The 🌐 Remote chip configures pairing; type
        # /remote-control by hand to bridge a session to claude.ai/code.)

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

    def _spawn_lead_when_quiet(self) -> None:
        """Quiet-boot debounce (Tier 1): sample gate once per event-loop turn.

        Each QTimer callback is one event-loop turn.  Streak resets when any
        condition fails: InSendMessageEx blocked, modal/popup active, or the
        application is not yet active+visible.  After _BOOT_LEAD_QUIET_N
        consecutive clear turns the gate is considered stable and Lead spawns.
        Tier 2 still performs the final adjacent re-check inside spawn().
        """
        from .spawn_gate import is_in_send_blocked

        modal_pred = getattr(self.orch, "_spawn_gate_pred", None)
        modal_clear = (modal_pred is None) or (not modal_pred())
        app_active = QApplication.applicationState() == Qt.ApplicationState.ApplicationActive
        window_ready = self.isVisible()
        insend_clear = not is_in_send_blocked()

        all_clear = insend_clear and modal_clear and app_active and window_ready

        if not all_clear:
            self._boot_quiet_count = 0
            _log_event(
                "boot_lead_gate_blocked",
                insend_clear=insend_clear,
                modal_clear=modal_clear,
                app_active=app_active,
                window_ready=window_ready,
            )
            QTimer.singleShot(_BOOT_LEAD_POLL_MS, self._spawn_lead_when_quiet)
            return

        self._boot_quiet_count += 1
        if self._boot_quiet_count < _BOOT_LEAD_QUIET_N:
            QTimer.singleShot(_BOOT_LEAD_POLL_MS, self._spawn_lead_when_quiet)
            return

        _log_event("boot_lead_spawn_ready", quiet_turns=self._boot_quiet_count)
        ok, msg = self.orch.spawn(LEAD.name)
        if not ok:
            self._status.showMessage(f"⚠ Lead spawn failed: {msg}", 30_000)

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

    def _notify_agent_done(self, project: str, role_name: str, note: str) -> None:
        """Show a desktop toast when an agent reports done."""
        title = f"{role_name} done"
        body = note.strip() if note else "task complete"
        self._status.showMessage(f"✓ {title}: {body[:80]}", 6_000)
        if self._tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 5_000)

    def _on_cross_tab_done(self, project_ns: str, role: str, note: str) -> None:
        """Flash the status bar when a background-tab teammate reports done.

        The user is currently viewing a different project tab — they'd otherwise
        see nothing. 8 s timeout gives them time to notice before it clears."""
        body = note.strip()[:80] if note.strip() else "task complete"
        self._status.showMessage(f"✓ [{role} done] in {project_ns}: {body}", 8_000)

    def _on_lead_notified(self, project_ns: str) -> None:
        """A notice was queued for `project_ns`'s Lead. Put an unread red dot on
        that project's Lead pane-tab unless the user is already looking at it —
        the panes-as-tabs layout shows one pane at a time, so a Lead handoff
        could otherwise scroll by unseen behind a teammate tab."""
        tab = self._tab_for_project(project_ns)
        if tab is not None:
            tab.mark_lead_unread()

    # ──────────────────────────────────────────────────────────────
    # toolbar buttons
    # ──────────────────────────────────────────────────────────────
    def _on_toggle_logs(self, checked: bool | None = None) -> None:
        # `checked` comes from a checkable button; when invoked from the Ctrl+L
        # shortcut (no button anymore) flip the dock's current visibility.
        if checked is None:
            checked = not self._logs_dock.isVisible()
        self._logs_dock.setVisible(checked)

    @staticmethod
    def _configure_tasks_dock_chrome(dock: QDockWidget) -> None:
        """Chrome setup for the Task List dock — factored out so a test can
        exercise it against a bare `QDockWidget()` without booting the full
        `MainWindow` (which needs a live orchestrator/CLI server).

        - No features at all (`NoDockWidgetFeatures`): `DockWidgetMovable`
          alone still let the user drag the dock out into a floating
          top-level window with OS min/max/close chrome (A8-regression item
          2 — `setFloating(False)` + the topLevelChanged safety-net weren't
          enough since the drag-to-float gesture doesn't go through either
          of those). The dock only ever lives in one place (its allowed
          areas are RightDockWidgetArea only, and it's the sole widget
          there), so movability between areas buys nothing; the user
          collapses it to a rail via the in-dock «  Collapse button instead
          (Ctrl+Shift+T still toggles hide/show, which doesn't need any
          dock feature — it's driven by `setVisible()`).
        - Blank titlebar widget: `QDockWidget("Task List", ...)`'s native
          titlebar duplicated `TaskDockWidget`'s own inner "Task List"
          header — a fixed-height-0 widget removes the native one entirely
          so only the inner header renders (A8-polish item 3).
        - `setFloating(False)`: paired with the topLevelChanged safety-net
          connected at the call site — belt-and-suspenders against the dock
          ever rendering as a top-level window with OS min/max/close chrome.
        """
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        blank_titlebar = QWidget()
        blank_titlebar.setFixedHeight(0)
        dock.setTitleBarWidget(blank_titlebar)
        dock.setFloating(False)

    def _on_toggle_tasks(self, checked: bool | None = None) -> None:
        if checked is None:
            checked = not self._tasks_dock.isVisible()
        self._tasks_dock.setVisible(checked)
        if checked:
            # Rebuild from disk on open — cheap (small JSON reads) and covers
            # any ledger write that landed while the dock was hidden (no
            # live-signal connection needed for that gap).
            self._tasks_dock_widget.refresh_all()

    def _on_tasks_dock_collapse_toggled(self, collapsed: bool) -> None:
        """Slide the Task List dock between its full width and the narrow
        avatar rail — mirrors project_nav's sidebar width tween, just applied
        to the QDockWidget instead of a plain child widget (TaskDockWidget
        only owns its own internal layout, not the dock's outer size)."""
        target = _TASKS_DOCK_COLLAPSED_W if collapsed else _TASKS_DOCK_EXPANDED_W
        start = self._tasks_dock.width()
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(self._tasks_dock, prop, self)
            anim.setDuration(190)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(anim)
        if not collapsed:
            # Expanding: release the temporary maximumWidth lock so the user
            # can still drag the dock wider afterwards (collapsed keeps
            # min == max == the rail width, deliberately non-resizable).
            group.finished.connect(lambda: self._tasks_dock.setMaximumWidth(16_777_215))
        self._tasks_dock_anim = group  # keep alive so PyQt doesn't GC it mid-run
        group.start()

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
            "F1 — this dialog<br>"
            "Ctrl + Shift + L — show/hide events log panel<br>"
            "Ctrl + Shift + T — show/hide task tree panel<br>"
            "Ctrl + + / - / 0 — terminal font size (click pane first)<br>"
            "Wheel — scroll claude's history (click pane first)<br><br>"
            "<b>Default cwd</b> when <code>--cwd</code> omitted: active project's<br>"
            "role-matched path (frontend→web, backend→api, ...)."
        )
        QMessageBox.information(self, "agent-takkub help", text)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F1:
            self._show_help()
            return
        super().keyPressEvent(event)

    def _install_shortcuts(self) -> None:
        from PyQt6.QtGui import QKeySequence, QShortcut

        QShortcut(QKeySequence(Qt.Key.Key_F1), self).activated.connect(self._show_help)
        # Ctrl+Shift+L toggles the events-log dock (replaces the removed 📋 Logs
        # button). NOT plain Ctrl+L — that's the terminal clear-screen inside a
        # focused claude/shell pane, and a window-level shortcut would shadow it.
        QShortcut(QKeySequence("Ctrl+Shift+L"), self).activated.connect(
            lambda: self._on_toggle_logs(None)
        )
        # Ctrl+Shift+T toggles the right-hand Task Tree dock (A8).
        QShortcut(QKeySequence("Ctrl+Shift+T"), self).activated.connect(
            lambda: self._on_toggle_tasks(None)
        )

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
        """Project names of every project currently open in the sidebar."""
        return [
            self.tabs.widget(i).project_name
            for i in range(self.tabs.count())
            if isinstance(self.tabs.widget(i), ProjectTab)
        ]

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
        """New-project entry point — a single dialog, 3 buttons:

          1. **เปิดโปรเจคที่ตั้งไว้** — open a project already configured in
             projects.json (excludes already-open ones).
          2. **โปรเจคใหม่ (AI rules)** — `_new_project_with_rules()`.
          3. **Import โฟลเดอร์** — `_import_existing_project()`.

        Collapses what used to be two sequential "new vs existing" dialogs
        (this one, then a second one asking "New with AI rules vs Import")
        into one — same three end behaviors, one fewer dialog hop.
        """
        from PyQt6.QtWidgets import QMessageBox

        open_names = set(self._open_projects())
        available = [n for n in list_project_names() if n not in open_names]

        box = QMessageBox(self)
        box.setWindowTitle("โปรเจคใหม่")
        box.setText("เปิดโปรเจคที่ตั้งไว้ หรือเพิ่มโปรเจคใหม่?")
        box.setInformativeText(
            "เลือกจากที่เคยตั้งไว้ในระบบ — หรือเพิ่มโปรเจคใหม่ (สร้างใหม่ด้วย AI rules / import โฟลเดอร์ที่มีอยู่)"
        )
        btn_existing = box.addButton("📂 เปิดโปรเจคที่ตั้งไว้", QMessageBox.ButtonRole.AcceptRole)
        btn_new = box.addButton("✨ โปรเจคใหม่ (AI rules)", QMessageBox.ButtonRole.AcceptRole)
        btn_import = box.addButton("📁 Import โฟลเดอร์", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("ยกเลิก", QMessageBox.ButtonRole.RejectRole)
        # No existing-but-unopened project → steer straight to "add new".
        if not available:
            btn_existing.setEnabled(False)
            btn_existing.setToolTip("ทุกโปรเจคที่มีถูกเปิดหมดแล้ว")
        box.exec()
        clicked = box.clickedButton()

        if clicked is btn_new:
            self._new_project_with_rules()
            return
        if clicked is btn_import:
            self._import_existing_project()
            return
        if clicked is not btn_existing:
            return  # cancelled / closed

        name, ok = QInputDialog.getItem(
            self,
            "เลือกโปรเจค",
            "เปิดโปรเจคไหนในแท็บใหม่:",
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
        # Guard: a project whose folder was deleted on disk would make Lead
        # spawn into a missing cwd. _pty_backend now raises instead of hanging,
        # but skip building a dead tab entirely and tell the user it's gone
        # (covers both boot tab-restore and the manual `+` picker).
        if not project_folder_exists(project_name):
            self._status.showMessage(
                f"⚠ project '{project_name}' folder is missing on disk — tab skipped. "
                f"Restore the folder or remove the project from the list.",
                15_000,
            )
            return
        # Set as active so spawn picks up lead_cwd() for the new project.
        set_active_project(project_name)
        self._refresh_project_list()

        tab = ProjectTab(project_name, lead_pane=None)
        idx = self.tabs.addTab(tab, project_name)
        self._wire_project_tab(tab)
        self.tabs.setCurrentIndex(idx)
        lead = AgentPane(LEAD, parent=tab)
        self.orch.register_pane(lead, project=project_name)
        tab.attach_lead(lead)

        ok, msg = self.orch.spawn(LEAD.name, project=project_name)
        if not ok:
            self._status.showMessage(f"⚠ Lead spawn failed for {project_name}: {msg}", 15_000)
            return
        lead._title.setText(f"Lead · {project_name}")
        # (The /remote-control auto-bridge was removed 2026-07-10 — type
        # /remote-control by hand to bridge a session to claude.ai/code.)
        self._refresh_rtk_button()
        if self._limit_store is not None:
            from . import user_profile as _up_ot

            self._limit_store.register(_up_ot.config_dir_for(project_name))
        self._persist_open_tabs()
        self._status.showMessage(f"opened tab · {project_name}", 4_000)

    def _on_tab_close_requested(self, index: int) -> None:
        """Close the tab at `index`. Confirms first, then delegates the
        actual teardown to `_close_project_tab`."""
        tab = self.tabs.widget(index)
        if not isinstance(tab, ProjectTab):
            return
        self._close_project_tab(tab.project_name, confirm=True)

    def _close_project_tab(self, project: str, confirm: bool = False) -> tuple[bool, str]:
        """Tear down every pane for `project` (Lead + teammates) and remove
        its tab. Shared by the desktop close-tab path (`confirm=True` shows
        the Qt dialog) and the remote `close_project` API (`confirm=False` —
        the phone confirms on its own side before calling in). If the last
        tab closes, the cockpit shows the empty state — the user can `+` a
        new one.

        Returns `(ok, message)`.
        """
        index = -1
        tab: ProjectTab | None = None
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ProjectTab) and w.project_name == project:
                index, tab = i, w
                break
        if tab is None:
            return False, f"no open tab for project '{project}'"

        if confirm:
            answer = QMessageBox.question(
                self,
                "Close tab",
                f"Close '{tab.project_name}' tab? Lead + every teammate pane for "
                f"this project will be terminated.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return False, "cancelled"

        self.orch.close_all_teammates(project=tab.project_name)
        self.orch.close(LEAD.name, project=tab.project_name, force=True, reason="tab_close")
        self.orch.unregister_pane(LEAD.name, project=tab.project_name, force=True)
        if self._limit_store is not None:
            from . import user_profile as _up_tc

            self._limit_store.unregister(_up_tc.config_dir_for(tab.project_name))
        # The usage meter is a single UsageMeter widget parked as this tab's
        # corner widget. If we're closing the tab that currently hosts it,
        # detach it BEFORE deleteLater — otherwise Qt destroys the C++ widget
        # along with the tab while Python keeps `_limit_label` pointing at the
        # dead wrapper, and every subsequent usage poll throws "has been
        # deleted", so the meter vanishes until the cockpit restarts. removeTab
        # below re-mounts
        # it on the new active tab via _on_tab_switched (host is None now, so it
        # skips the stale-clear and just mounts).
        if self._limit_label_host is tab:
            tab.pane_tabs.setCornerWidget(None, Qt.Corner.TopRightCorner)
            self._limit_label.setParent(None)
            self._limit_label_host = None
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
        return True, f"closed tab · {tab.project_name}"

    def _on_tab_switched(self, index: int) -> None:
        """User picked a different project in the sidebar. Sync `active` in
        projects.json so the orchestrator's project-default resolution and the
        rtk button match the visible project."""
        if index < 0:
            # No tabs left (e.g. the last one just closed) — `active` must
            # not keep pointing at a project with no open tab (#102).
            clear_active_project()
            return
        tab = self.tabs.widget(index)
        if not isinstance(tab, ProjectTab):
            return
        # Only the visible project keeps painting; every hidden project suspends
        # its panes' paint keep-alive so their Chromium renderers can release
        # compositor memory (fix for backgrounded renderers ballooning to GBs).
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ProjectTab):
                w.set_keepalive(i == index)
        # Reparent the usage label: clear the old corner first so the previous
        # tab doesn't keep a stale reference, then mount on the new active tab.
        if self._limit_label_host is not None and self._limit_label_host is not tab:
            self._limit_label_host.pane_tabs.setCornerWidget(None, Qt.Corner.TopRightCorner)
        self._limit_label_host = tab
        tab.mount_usage_widget(self._limit_label)
        if set_active_project(tab.project_name):
            self._refresh_rtk_button()
            from . import user_profile as _up_sw

            if self._limit_store is not None:
                cd = _up_sw.config_dir_for(tab.project_name)
                self._refresh_limit_label(self._limit_store.get(cd))
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
        # 2. Close Lead — force=True because this is an intentional kill-then-respawn.
        self.orch.close(LEAD.name, force=True, reason="restart_lead")
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
        # (The /remote-control auto-bridge was removed 2026-07-10 — type
        # /remote-control by hand to bridge a session to claude.ai/code.)

    def _on_tab_context_menu(self, index: int, global_pos) -> None:
        """Right-click a sidebar project → project-level actions."""
        from PyQt6.QtWidgets import QMenu

        widget = self.tabs.widget(index)
        if not isinstance(widget, ProjectTab):
            return
        proj_name = widget.project_name

        menu = QMenu(self)
        act_edit = menu.addAction("✏️ Edit project…")
        act_rules = menu.addAction("📋 Edit project rules…")
        menu.addSeparator()
        act_close = menu.addAction("🗙 Close project")
        chosen = menu.exec(global_pos)
        if chosen is act_edit:
            self._on_edit_project_clicked(proj_name)
        elif chosen is act_rules:
            self._on_edit_project_rules_clicked(proj_name)
        elif chosen is act_close:
            self._on_tab_close_requested(index)

    # ──────────────────────────────────────────────────────────────
    # window state persistence
    # ──────────────────────────────────────────────────────────────
    def _restore_window_state(self) -> None:
        geo = self._settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_window_state(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.sync()

    def _tick_heartbeat(self) -> None:
        self._heartbeat_ts = time.monotonic()

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

        # Background _DoctorThread / _PluginInstallThread workers are held at
        # module level (user_actions._DOCTOR_THREADS/_PLUGIN_THREADS), NOT
        # parented to this window, so window teardown can't destroy a running
        # one. No wait needed — they finish in the background and the process
        # reaps them on exit. (A Doctor git fetch or a 120s-per-plugin install
        # would have blown any bounded close-time wait anyway.)

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
                    # wait=True: tear down inline on app exit so taskkill /T
                    # finishes before the process dies — a detached daemon
                    # teardown would be killed mid-kill and orphan the tree.
                    pane.session.terminate(wait=True)
        if self._limit_store is not None:
            self._limit_store.stop()
            self._limit_store = None
        # Don't rely on app.aboutToQuit alone (app.py::_kill_all / the
        # remote module's own hookup both connect to it) — a system tray
        # icon or another live top-level widget can keep the QApplication
        # event loop alive past this window closing, in which case
        # aboutToQuit never fires and the remote HTTP server's daemon
        # thread keeps serving on a process that looks closed to the user.
        # stop() is idempotent (disconnects its own aboutToQuit hookup
        # first), so calling it here is safe even if aboutToQuit does
        # still fire afterwards.
        try:
            if self._remote is not None:
                self._remote.stop()
        except Exception:
            pass
        self.cli.close()
        super().closeEvent(event)


# DEFAULT_TEAMMATES is imported only so the symbol stays referenced (for the
# CLI tab-completion / future role picker UI). Suppress unused warning.
_ = DEFAULT_TEAMMATES
