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

from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStyle,
    QSystemTrayIcon,
    QWidget,
)

from .agent_pane import AgentPane
from .cli_server import CliServer
from .config import (
    EVENTS_LOG,
    active_project,
    get_open_tabs,
    list_project_names,
    preset_roles_for_active,
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
from .update_panel import MainWindowUpdateMixin
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
        self.orch.paneResumed.connect(self._on_pane_resumed)
        self.orch.crossTabDone.connect(self._on_cross_tab_done)
        self.orch.leadNotified.connect(self._on_lead_notified)
        # ── Office Room game bridge ──────────────────────────────
        self.orch.paneRequested.connect(self._game_on_pane_requested)
        self.orch.paneClosed.connect(self._game_on_pane_closed)
        self.orch.agentDone.connect(self._game_on_agent_done)
        self.orch.statusChanged.connect(self._game_sync_all_states)

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
        # Tracks which Lead panes have already auto-bridged `/remote-control`
        # in this cockpit session — keyed by project name. Populated by:
        #   - `_on_pane_resumed` (session-resume auto-fire)
        #   - `_on_lead_input` (first user Enter in Lead pane)
        # Membership prevents double-firing across both paths.
        self._lead_first_input_fired: set[str] = set()

        initial_project = active_project()[0] or "default"
        initial_tab = ProjectTab(initial_project, lead_pane=None)
        self.tabs.addTab(initial_tab, initial_project)
        self._wire_project_tab(initial_tab)
        initial_lead = AgentPane(LEAD, parent=initial_tab)
        self.orch.register_pane(initial_lead, project=initial_project)
        initial_tab.attach_lead(initial_lead)
        initial_lead.inputBytes.connect(
            lambda role, data, proj=initial_project: self._on_lead_input(proj, role, data)
        )
        self.tabs.setCurrentIndex(0)

        # Usage-window readout — sits as the corner widget of the active
        # project's pane_tabs (top-right, same row as the Lead/teammate tabs).
        # On every project switch _on_tab_switched reparents this single label
        # into the new active ProjectTab's corner via mount_usage_widget().
        self._limit_label = QLabel("—")
        self._limit_label.setStyleSheet(
            "QLabel { color:#52525b; font-size:11px; "
            "font-variant-numeric:tabular-nums; padding:0 8px; }"
        )
        self._limit_label.setToolTip(
            "Claude usage windows (5h / 7d / 7d-Sonnet)\n"
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

        # ── bottom logs dock (hidden by default) ────────────────
        self._logs_dock = QDockWidget("events", self)
        self._logs_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._logs_dock.setWidget(LogsPanel(EVENTS_LOG))
        self._logs_dock.setMinimumHeight(140)
        self._logs_dock.hide()
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._logs_dock)

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
        # Populate the version chip immediately so the user doesn't see
        # an empty slot for the first 30 s until the update check runs.
        self._refresh_version_label()

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
        tab.focusRoleRequested.connect(lambda role, t=tab: self._game_on_focus_role(role, t))
        tab.leadClickedInGame.connect(lambda t=tab: self._game_on_focus_role("lead", t))
        tab.messageToLead.connect(
            lambda msg, proj=tab.project_name: self.orch.inject_lead_prompt(msg, project=proj)
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
            color = tab.custom_role_colors.get(role_name, "#94a3b8")
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
        pane = tab.remove_teammate_tab(role_name)
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

    # ──────────────────────────────────────────────────────────────
    # Office Room game bridge helpers
    # ──────────────────────────────────────────────────────────────
    def _game_dispatch(self, role: str, state: str, project: str = "", note: str = "") -> None:
        """Route a game event to the correct ProjectTab's OfficeRoomView."""
        tab = self._tab_for_project(project) if project else self._current_tab()
        if tab is None:
            return
        tab.dispatch_game_event(role, state, note=note, project=project)

    def _game_on_pane_requested(self, role_name: str, project: str) -> None:
        self._game_dispatch(role_name, "spawn", project=project)

    def _game_on_pane_closed(self, role_name: str, project: str) -> None:
        self._game_dispatch(role_name, "close", project=project)

    def _game_on_agent_done(self, project: str, role_name: str, note: str) -> None:
        self._game_dispatch(role_name, "done", project=project, note=note)

    def _game_sync_all_states(self) -> None:
        """On every statusChanged (and on game-view toggle-in), push busy/idle
        state for every alive pane so the scene stays in sync."""
        for project, panes in list(self.orch._panes_by_project.items()):
            tab = self._tab_for_project(project)
            if tab is None:
                continue
            for role, pane in list(panes.items()):
                if pane.session is None or not pane.session.is_alive:
                    continue
                state = "busy" if pane.state == "working" else "idle"
                tab.dispatch_game_event(role, state, project=project)

    def _game_on_focus_role(self, role: str, tab: ProjectTab) -> None:
        """Switch the cockpit to the pane-tab for `role` in `tab`."""
        pane = tab.teammate_panes.get(role)
        if pane is None and role == "lead":
            pane = tab.lead_pane
        if pane is None:
            return
        idx = tab.pane_tabs.indexOf(pane)
        if idx >= 0:
            tab.pane_tabs.setCurrentIndex(idx)
        # Also switch back to text view
        if tab.is_game_active():
            tab.toggle_game_view()
            self._btn_game_view.setChecked(False)
            self._btn_game_view.setText("🎮")

    # ──────────────────────────────────────────────────────────────
    def _boot(self) -> None:
        try:
            port = self.cli.listen()
            self._status.showMessage(f"cli port {port} — waiting for quiet boot...")
        except Exception as e:
            _handle_cli_bind_error(str(e))
            return  # QApplication.quit() is pending; don't proceed

        # Reflect orchestrator errors (eg. claude.exe not found) into the
        # status bar instead of swallowing them silently.
        self.orch.paneRequested.connect(self._track_pane_request)

        # Surface label "Lead · <project>" immediately (doesn't need spawn).
        active = active_project()[0]
        if active:
            self.lead_pane._title.setText(f"Lead · {active}")

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

        # No auto-/remote-control on fresh boot. The cockpit listens for
        # orch.paneResumed and only injects the bridge command on session
        # resume (i.e. the Lead was respawned with --continue inside the
        # 5-minute window). Manual trigger via the clickable hint chip
        # below for fresh boots when the user wants to drive from a phone.

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

    def _on_pane_resumed(self, role: str, project: str) -> None:
        """Auto-bridge `/remote-control` when a Lead pane was actually
        *resumed* (spawn picked up --continue). Teammate-pane resumes
        are ignored — they're spawned to do task work, not to bridge."""
        if role != LEAD.name:
            return
        if project in self._lead_first_input_fired:
            return
        self._lead_first_input_fired.add(project)
        self.orch.inject_slash_command_when_ready(LEAD.name, "/remote-control", project=project)

    def _on_lead_input(self, project: str, role: str, data: bytes) -> None:
        """First-task auto-bridge: when the user hits Enter in a Lead pane
        for the first time this session, fire `/remote-control` so the
        bridge command lands right after their task. Fresh boot + idle
        Lead → silent. Click the hint chip to bridge without typing.
        """
        if role != LEAD.name:
            return
        if project in self._lead_first_input_fired:
            return
        # Treat any Enter (CR or LF) as "task submitted". False positives
        # (user pressed Enter on empty input) are benign — claude no-ops
        # on /remote-control when already active.
        if b"\r" not in data and b"\n" not in data:
            return
        self._lead_first_input_fired.add(project)
        self.orch.inject_slash_command_when_ready(LEAD.name, "/remote-control", project=project)

    # ──────────────────────────────────────────────────────────────
    # toolbar buttons
    # ──────────────────────────────────────────────────────────────
    def _on_toggle_logs(self, checked: bool) -> None:
        self._logs_dock.setVisible(checked)

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
            "F1 / ? button — this dialog<br>"
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
        idx = self.tabs.addTab(tab, project_name)
        self._wire_project_tab(tab)
        self.tabs.setCurrentIndex(idx)
        lead = AgentPane(LEAD, parent=tab)
        self.orch.register_pane(lead, project=project_name)
        tab.attach_lead(lead)
        lead.inputBytes.connect(
            lambda role, data, proj=project_name: self._on_lead_input(proj, role, data)
        )

        ok, msg = self.orch.spawn(LEAD.name, project=project_name)
        if not ok:
            self._status.showMessage(f"⚠ Lead spawn failed for {project_name}: {msg}", 15_000)
            return
        lead._title.setText(f"Lead · {project_name}")
        # No auto-/remote-control on tab open. Resume case is handled by
        # the orch.paneResumed signal; manual case via the hint chip.
        self._refresh_rtk_button()
        if self._limit_store is not None:
            from . import user_profile as _up_ot

            self._limit_store.register(_up_ot.config_dir_for(project_name))
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
        self.orch.close(LEAD.name, project=tab.project_name, force=True, reason="tab_close")
        self.orch.unregister_pane(LEAD.name, project=tab.project_name, force=True)
        if self._limit_store is not None:
            from . import user_profile as _up_tc

            self._limit_store.unregister(_up_tc.config_dir_for(tab.project_name))
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
        """User picked a different project in the sidebar. Sync `active` in
        projects.json so the orchestrator's project-default resolution and the
        rtk button match the visible project."""
        if index < 0:
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
            self._limit_label_host.pane_tabs.setCornerWidget(
                None, Qt.Corner.TopRightCorner
            )
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
        # Resume after self-update restart: orch.paneResumed handles the
        # `/remote-control` injection automatically because spawn() above
        # picks up --continue inside the 5-minute resume window.

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
        if self._limit_store is not None:
            self._limit_store.stop()
            self._limit_store = None
        self.cli.close()
        super().closeEvent(event)


# DEFAULT_TEAMMATES is imported only so the symbol stays referenced (for the
# CLI tab-completion / future role picker UI). Suppress unused warning.
_ = DEFAULT_TEAMMATES
