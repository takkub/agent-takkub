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

import json
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QSettings, Qt, QThread, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
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
    PORT_FILE,
    REPO_ROOT,
    _write_json_atomic,
    active_project,
    get_open_tabs,
    lead_cwd,
    list_project_names,
    preset_roles_for_active,
    set_active_project,
    set_open_tabs,
)
from .logs_panel import LogsPanel
from .orchestrator import Orchestrator, _log_event
from .project_tab import ProjectTab
from .roles import DEFAULT_TEAMMATES, LEAD, Role, by_name
from .rtk_helper import install_rtk, is_rtk_installed, rtk_binary_available
from .token_meter import format_tokens, usage_color


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


class _RulesGeneratorThread(QThread):
    """Background thread that runs claude headless to generate project rules.

    Signals
    -------
    finished(str)  — emits the generated markdown on success
    failed(str)    — emits an error message on failure (incl. cancel)
    """

    rulesReady: pyqtSignal = pyqtSignal(str)
    failed: pyqtSignal = pyqtSignal(str)

    def __init__(self, prompt: str, project_name: str, parent=None) -> None:
        super().__init__(parent)
        self._prompt = prompt
        self._project_name = project_name
        self._proc = None  # subprocess.Popen — set in run(), cleared after

    def run(self) -> None:
        from .project_rules import collect_result, generate_project_rules_proc

        try:
            proc = generate_project_rules_proc(self._prompt, self._project_name)
            self._proc = proc
            content = collect_result(proc, self._project_name)
            self._proc = None
            self.rulesReady.emit(content)
        except Exception as exc:
            self._proc = None
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        """Kill the claude subprocess if it's still running."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


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

    @staticmethod
    def _make_status_separator() -> QFrame:
        """A thin vertical divider between status-bar groups.

        Same height as the chips around it so the bar still reads as a
        single row; faint zinc color so it acts as a hint, not a wall.
        """
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setFixedWidth(1)
        sep.setStyleSheet("QFrame { color: #3f3f46; background: #3f3f46; }")
        sep.setContentsMargins(2, 4, 2, 4)
        return sep

    @staticmethod
    def _provider_chip_style(provider: str, disabled: bool) -> str:
        """Outline chip for the codex/gemini toggles.

        The leading status dot (baked into the button text) inherits the
        text color — brand when enabled, gray + strikethrough when disabled
        — so on/off reads at a glance, while the outline + hover keep it
        obviously a clickable toggle (the old filled-pill looked like a
        plain action button → affordance mismatch).
        """
        if disabled:
            return (
                "QPushButton { "
                "background:transparent; color:#71717a; "
                "border:1px solid #3f3f46; border-radius:10px; "
                "padding:2px 10px; font-weight:500; "
                "text-decoration: line-through; "
                "}"
                "QPushButton:hover { background:#27272a; color:#a1a1aa; }"
            )
        # Brand colors: codex teal (#10a37f) / gemini blue (#4285f4)
        brand = "#10a37f" if provider == "codex" else "#4285f4"
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:10px; "
            "padding:2px 10px; font-weight:600; "
            "}"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _plan_chip_style(is_pro: bool) -> str:
        """Outline chip for the account plan (Pro/Max).

        Two valid modes (not on/off), so no strikethrough. Outline (not the
        old solid fill) keeps it consistent with the provider chips and
        signals a clickable mode-toggle without shouting. Max = violet
        (full access incl. 1M context), Pro = amber (1M capped).
        """
        brand = "#8b5cf6" if not is_pro else "#f59e0b"
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:10px; "
            "padding:2px 10px; font-weight:600; "
            "}"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _ghost_button_style() -> str:
        """Neutral status-bar action button.

        Quiet by default so the bar reads calm and the one accented button
        (End Session, destructive) carries the visual weight. Replaces the
        old per-button rainbow fills (every button shouted equally →
        Christmas-tree effect, no hierarchy). `:checked` covers the
        toggle-style Logs button.
        """
        return (
            "QPushButton { color:#d4d4d8; background:transparent; "
            "border:1px solid #3f3f46; border-radius:4px; padding:2px 8px; }"
            "QPushButton:hover { background:#27272a; border-color:#52525b; }"
            "QPushButton:checked { background:#27272a; color:#e4e4e7; border-color:#52525b; }"
        )

    @staticmethod
    def _danger_button_style() -> str:
        """Restrained red accent for the one consequential action.

        End Session closes every teammate pane — the most destructive
        status-bar action — so it gets the only colored treatment. Outline,
        not a full red fill, so it stands out against the ghost buttons
        without re-introducing the rainbow.
        """
        return (
            "QPushButton { color:#fca5a5; background:transparent; "
            "border:1px solid #7f1d1d; border-radius:4px; padding:2px 8px; }"
            "QPushButton:hover { background:#450a0a; border-color:#b91c1c; }"
        )

    @staticmethod
    def _plan_chip_tooltip(is_pro: bool) -> str:
        """Tooltip for the plan chip — explains the consequence, not just the state."""
        if is_pro:
            return (
                "Account plan: Pro — click to switch to Max.\n"
                "New Lead panes pin to a standard-context model\n"
                "(1M context is usage-credits gated on Pro)."
            )
        return (
            "Account plan: Max — click to switch to Pro.\n"
            "Lead inherits your default model, incl. 1M context."
        )

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
        self.orch.paneResumed.connect(self._on_pane_resumed)
        self.orch.crossTabDone.connect(self._on_cross_tab_done)

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
        # Right-click on any project tab → context menu with "Edit project rules"
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._on_tab_bar_context_menu)
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
        # Tracks which Lead panes have already auto-bridged `/remote-control`
        # in this cockpit session — keyed by project name. Populated by:
        #   - `_on_pane_resumed` (session-resume auto-fire)
        #   - `_on_lead_input` (first user Enter in Lead pane)
        #   - `_on_remote_hint_clicked` (manual chip click)
        # Membership prevents double-firing across the three paths.
        self._lead_first_input_fired: set[str] = set()

        initial_project = active_project()[0] or "default"
        initial_tab = ProjectTab(initial_project, lead_pane=None)
        self.tabs.addTab(initial_tab, initial_project)
        initial_lead = AgentPane(LEAD, parent=initial_tab)
        self.orch.register_pane(initial_lead, project=initial_project)
        initial_tab.attach_lead(initial_lead)
        initial_lead.inputBytes.connect(
            lambda role, data, proj=initial_project: self._on_lead_input(proj, role, data)
        )

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
        self._btn_add_project.setToolTip(
            "Add project — choose New (AI-generated CLAUDE.md rules) or Import existing"
        )
        self._btn_add_project.setFixedWidth(28)
        self._btn_add_project.clicked.connect(self._on_add_project_clicked)

        self._btn_edit_rules = QPushButton("📋", self)
        self._btn_edit_rules.setToolTip("Edit project rules (CLAUDE.md) for the active project")
        self._btn_edit_rules.setFixedWidth(28)
        self._btn_edit_rules.clicked.connect(self._on_edit_project_rules_clicked)

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
        # Amber color + emoji is enough nudge — keep the button visible
        # without screaming. Original 2px border + bold made it look like
        # the cockpit's primary CTA when it's actually optional optimisation.
        self._btn_install_rtk.setStyleSheet(
            "QPushButton { color: #000; background: #fbbf24; "
            "border: 1px solid #b45309; border-radius: 4px; "
            "padding: 2px 8px; font-size: 12px; }"
            "QPushButton:hover { background: #fcd34d; }"
        )
        self._btn_install_rtk.clicked.connect(self._on_install_rtk_clicked)

        # ── provider toggle chips (codex / gemini) ─────────────────
        # State source-of-truth lives in provider_state.json; orchestrator
        # owns the broadcast on toggle. We just create the buttons and
        # subscribe to providerStateChanged to redraw.
        from .provider_state import CODEX, GEMINI, is_disabled

        self._chip_codex = QPushButton("● Codex", self)
        self._chip_codex.setToolTip(
            "Codex: disabled — click to enable"
            if is_disabled(CODEX)
            else "Codex: enabled — click to disable"
        )
        self._chip_codex.setStyleSheet(self._provider_chip_style(CODEX, is_disabled(CODEX)))
        self._chip_codex.clicked.connect(lambda: self._on_provider_chip_clicked(CODEX))

        self._chip_gemini = QPushButton("● Gemini", self)
        self._chip_gemini.setToolTip(
            "Gemini: disabled — click to enable"
            if is_disabled(GEMINI)
            else "Gemini: enabled — click to disable"
        )
        self._chip_gemini.setStyleSheet(self._provider_chip_style(GEMINI, is_disabled(GEMINI)))
        self._chip_gemini.clicked.connect(lambda: self._on_provider_chip_clicked(GEMINI))

        # ── account plan chip (Pro / Max) ──────────────────────────
        # Records whether the owner is on Pro or Max so the orchestrator can
        # pin the Lead to a standard-context model under Pro (the 1M-context
        # variant is usage-credits gated and hard-errors on Pro). State lives
        # in plan.json; orchestrator owns persist+broadcast on flip.
        from .plan_tier import is_pro as _plan_is_pro

        _pro_now = _plan_is_pro()
        self._chip_plan = QPushButton("Pro" if _pro_now else "Max", self)
        self._chip_plan.setToolTip(self._plan_chip_tooltip(_pro_now))
        self._chip_plan.setStyleSheet(self._plan_chip_style(_pro_now))
        self._chip_plan.clicked.connect(self._on_plan_chip_clicked)

        # Self-update chip. Polls `git fetch` + `git status` every 5 min
        # so a user that pulled their friend's commit from another machine
        # sees the update light up here without needing to touch a
        # terminal. Click flow lives in `_on_update_clicked` (with a
        # confirm dialog, dirty-tree guard, and restart-on-success).
        # Neutral placeholder until the first poll completes (~30 s after
        # boot). Showing "Up to date" pre-check is a lie — if the user
        # clicks during the window before the timer fires, the cache is
        # still None and the click handler can't render a real status.
        self._btn_update = QPushButton("🔄 Checking…", self)
        self._btn_update.setToolTip(
            "Check for cockpit code updates from origin/main and pull them\n"
            "via fast-forward. User-specific files (projects.json, runtime/,\n"
            ".venv/) are gitignored and never touched. Local edits to\n"
            "tracked files block the pull until you commit or stash."
        )
        self._btn_update.setStyleSheet(
            "QPushButton { color: #71717a; background: transparent; "
            "border: none; padding: 2px 6px; font-size: 12px; }"
            "QPushButton:hover { color: #94a3b8; }"
        )
        self._btn_update.clicked.connect(self._on_update_clicked)
        # Cached result from the most recent poll. Populated lazily so
        # the click handler doesn't re-run git just to re-render the
        # same dialog.
        self._update_status_cache: dict | None = None
        # True while a background UpdateCheckWorker is running; prevents
        # queuing a second fetch before the first one completes.
        self._update_worker_busy: bool = False

        # ── Claude CLI update button ───────────────────────────────
        # Separate from _btn_update (which pulls agent-takkub source). This
        # one updates the Claude Code CLI (`@anthropic-ai/claude-code` via
        # npm) and, before applying, runs an AI compatibility check against
        # how the cockpit spawns claude. Flow: _on_claude_update_clicked →
        # background worker (version + changelog + analysis) → report dialog
        # → confirm → close live claude panes (Windows lock guard) → npm
        # install → restart prompt.
        self._btn_claude_update = QPushButton("⬆ Claude CLI", self)
        self._btn_claude_update.setToolTip(
            "ตรวจว่ามี Claude Code CLI version ใหม่ไหม\n"
            "ถ้ามี: วิเคราะห์ด้วย AI ว่าใช้กับ cockpit ได้ไหม → ยืนยัน → อัพเดต\n"
            "(ก่อนอัพเดตจะปิด claude pane ที่รันอยู่ กัน brick บน Windows)"
        )
        self._btn_claude_update.setStyleSheet(self._ghost_button_style())
        self._btn_claude_update.clicked.connect(self._on_claude_update_clicked)
        # True while ClaudeUpdateCheckWorker runs — blocks re-entry.
        self._claude_update_busy: bool = False

        self._btn_help = QPushButton("?", self)
        self._btn_help.setToolTip("Show keyboard shortcuts and takkub CLI reference (also F1)")
        self._btn_help.setStyleSheet(self._ghost_button_style())
        self._btn_help.clicked.connect(self._show_help)

        self._btn_logs = QPushButton("📋 Logs", self)
        self._btn_logs.setToolTip("Show/hide events log panel")
        self._btn_logs.setCheckable(True)
        self._btn_logs.setStyleSheet(self._ghost_button_style())
        self._btn_logs.clicked.connect(self._on_toggle_logs)

        self._btn_restart = QPushButton(self)
        self._btn_restart.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._btn_restart.setToolTip("Restart cockpit (kills all panes, relaunches app)")
        self._btn_restart.clicked.connect(self._on_restart_cockpit_clicked)

        self._btn_resume = QPushButton("↻ Resume", self)
        self._btn_resume.setToolTip(
            "Send /resume to the Lead pane — opens claude's session picker\n"
            "so you can hop back into a previous conversation."
        )
        self._btn_resume.setStyleSheet(self._ghost_button_style())
        self._btn_resume.clicked.connect(self._on_resume_clicked)

        # End-Session button: prompts for a session-summary note then runs
        # close_all_teammates + end_session. The summary feeds Lead's
        # spawn-time "Recent session brief" the next time a pane opens for
        # this project — so finishing through this button is what makes
        # the next session "remember" what just happened.
        self._btn_end_session = QPushButton("🏁 End Session", self)
        self._btn_end_session.setToolTip(
            "Wrap up the active project: prompts for a one-paragraph note,\n"
            "closes every teammate pane, then writes a Lead session summary\n"
            "to runtime/sessions/ + the vault. Next session for this project\n"
            "auto-inherits the note in Lead's spawn-time prompt."
        )
        self._btn_end_session.setStyleSheet(self._danger_button_style())
        self._btn_end_session.clicked.connect(self._on_end_session_clicked)

        # Bug-Check button: broadcasts an introspection prompt to every
        # active pane. Each agent decides on its own whether to file a
        # cockpit issue or report "no bugs" back to Lead. Keeps bug
        # capture cheap — user doesn't have to context-switch to write
        # an issue while still mid-task.
        self._btn_bug_check = QPushButton("🐛 Bug Check", self)
        self._btn_bug_check.setToolTip(
            "Ask every active pane to introspect this session for cockpit\n"
            "bugs (orchestrator / CLI / UI). Each agent runs\n"
            "`takkub issue new` if it found something, or sends a clean\n"
            "report back to Lead. Project-scoped — other tabs untouched."
        )
        self._btn_bug_check.setStyleSheet(self._ghost_button_style())
        self._btn_bug_check.clicked.connect(self._on_bug_check_clicked)

        # 🎨 UI Review button: 1-click design-review pipeline. Spawns critic +
        # gemini in parallel. Critic reads shots from runtime/exports/<date>/
        # <project>/screenshots/ (left by QA's `mb shot` runs) and writes a
        # proposal to docs/design-review/. Gemini cross-checks visually so
        # the proposal isn't single-agent confirmation bias.
        self._btn_ui_review = QPushButton("🎨 UI Review", self)
        self._btn_ui_review.setToolTip(
            "Run the design-review pipeline: spawn critic + gemini parallel\n"
            "to read today's QA screenshots and propose add/remove/refine.\n"
            "Fire after QA smoke; proposals land in docs/design-review/."
        )
        self._btn_ui_review.setStyleSheet(self._ghost_button_style())
        self._btn_ui_review.clicked.connect(self._on_ui_review_clicked)

        # 💻 Open Shell: drops a raw PowerShell into the cockpit grid as
        # the `shell` pane. No claude, no codex, no gemini — just pwsh in
        # the active project's cwd. Lets the user run a one-off git poke
        # or tail a log without leaving the cockpit. Re-clicking when the
        # pane already exists just focuses it (orchestrator.spawn() is a
        # no-op for an already-running session).
        self._btn_open_shell = QPushButton("💻 Shell", self)
        self._btn_open_shell.setToolTip(
            "Open a PowerShell pane inside this project's cockpit grid.\n"
            "Lands in the active project's cwd. Close like any other pane\n"
            "(header × button or `exit`). Re-clicking focuses the existing pane."
        )
        self._btn_open_shell.setStyleSheet(self._ghost_button_style())
        self._btn_open_shell.clicked.connect(self._on_open_shell_clicked)

        self._btn_pipelines = QPushButton("⚙ Pipelines", self)
        self._btn_pipelines.setToolTip(
            "Build dev pipelines: drag roles into hops, save reusable templates,\n"
            "toggle providers, enable/disable roles, and pick each role's CLI\n"
            "(claude/codex/gemini). Edits ~/.takkub/pipelines.json +\n"
            "disabled-providers.json + role-providers.json. Applies to the\n"
            "next pane you spawn, no restart needed."
        )
        self._btn_pipelines.setStyleSheet(self._ghost_button_style())
        self._btn_pipelines.clicked.connect(self._on_pipelines_clicked)

        self._btn_claude_auth = QPushButton("Claude Auth", self)
        self._btn_claude_auth.setToolTip(
            "Configure optional Claude Code base URL / API key / auth token overrides.\n"
            "Leave fields blank to use Claude Code's default login/session."
        )
        self._btn_claude_auth.setStyleSheet(self._ghost_button_style())
        self._btn_claude_auth.clicked.connect(self._on_claude_auth_clicked)
        # Hidden per user request (kept for easy future restore). It's created +
        # wired but NOT added to the status bar; hide() so an unplaced child
        # doesn't render as a stray button at the window origin. To restore:
        # delete this hide() line and re-add it to the Group-3 widget tuple below.
        self._btn_claude_auth.hide()

        # Clickable /remote-control trigger. The built-in Claude Code command
        # bridges a local session to claude.ai/code for browser/phone
        # control. The cockpit no longer auto-fires it on fresh project
        # opens (was noisy — every tab open spammed the bridge). Now it
        # fires only on resume (via orch.paneResumed) or when the user
        # clicks this chip directly.
        self._remote_hint = QLabel("💡 /remote-control · click to bridge", self)
        self._remote_hint.setStyleSheet(
            "color: #fbbf24; font-size: 11px; padding: 0 8px; "
            "background: rgba(251, 191, 36, 0.08); border-radius: 4px;"
        )
        self._remote_hint.setToolTip(
            "Click to run /remote-control inside the active Lead pane.\n"
            "Bridges this Claude session to claude.ai/code so you can\n"
            "continue from a browser or phone. Auto-fires on session\n"
            "resume; this chip is for manual trigger on a fresh boot."
        )
        self._remote_hint.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remote_hint.mousePressEvent = lambda _ev: self._on_remote_hint_clicked()

        # Cockpit version chip: shows `v<pyproject> · @<short-sha>` so the
        # user can see at a glance what build they're on. Refreshed after
        # every update check + after a successful pull.
        self._version_label = QLabel("", self)
        self._version_label.setStyleSheet(
            "color: #6b7280; font-size: 11px; padding: 0 6px; font-variant-numeric: tabular-nums;"
        )
        self._version_label.setToolTip(
            "Cockpit version + commit SHA.\nClick to view the changelog "
            "(copy version from inside).\nClick the 🔄 chip to pull updates."
        )
        self._version_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._version_label.mousePressEvent = lambda _ev: self._show_changelog()

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

        # `project_combo` is retained as a hidden widget so legacy code
        # paths that update it (`_refresh_project_list`, `_on_project_changed`)
        # still link cleanly. The tab strip is now the authoritative
        # project switcher — the combo would just duplicate it visually.
        self._project_combo.hide()

        # Status bar is laid out in 3 semantic groups separated by thin
        # vertical lines. Without grouping, 14+ widgets scan as one long
        # blob and the user has to recall (not recognize) which button
        # does what. Order within each group stays stable across cockpit
        # versions so muscle memory survives upgrades.
        #
        #   Group 1 — Project context  (info you read, not click)
        #   Group 2 — Workflow actions (buttons that change pane state)
        #   Group 3 — System status    (cockpit-level toggles + updates)
        for w in (
            self._remote_hint,
            self._version_label,
            self._token_total,
            self._btn_add_project,
            self._btn_edit_rules,
        ):
            self._status.addPermanentWidget(w)
        self._status.addPermanentWidget(self._make_status_separator())
        for w in (
            self._btn_help,
            self._btn_logs,
            self._btn_resume,
            self._btn_open_shell,
            self._btn_bug_check,
            self._btn_ui_review,
            self._btn_end_session,
        ):
            self._status.addPermanentWidget(w)
        self._status.addPermanentWidget(self._make_status_separator())
        for w in (
            self._chip_plan,
            self._chip_codex,
            self._chip_gemini,
            self._btn_install_rtk,
            self._btn_restart,
            self._btn_pipelines,
            # self._btn_claude_auth,  # hidden per user request — uncomment to restore.
            # The button + its handler are still created above; only its
            # placement in the status bar is removed so it can come back easily.
            self._btn_claude_update,
            self._btn_update,
        ):
            self._status.addPermanentWidget(w)
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
        self.orch.providerStateChanged.connect(self._on_provider_state_changed)
        self.orch.planTierChanged.connect(self._on_plan_tier_changed)

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

    # ──────────────────────────────────────────────────────────────
    def _boot(self) -> None:
        try:
            port = self.cli.listen()
            self._status.showMessage(f"cli port {port}, spawning Lead...")
        except Exception as e:
            _handle_cli_bind_error(str(e))
            return  # QApplication.quit() is pending; don't proceed

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

    def _on_cross_tab_done(self, project_ns: str, role: str, note: str) -> None:
        """Flash the status bar when a background-tab teammate reports done.

        The user is currently viewing a different project tab — they'd otherwise
        see nothing. 8 s timeout gives them time to notice before it clears."""
        body = note.strip()[:80] if note.strip() else "task complete"
        self._status.showMessage(f"✓ [{role} done] in {project_ns}: {body}", 8_000)

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

    def _on_remote_hint_clicked(self) -> None:
        """Manual `/remote-control` trigger via the status-bar hint chip.
        Targets the currently-focused tab's Lead pane."""
        tab = self._current_tab()
        if tab is None or tab.lead_pane is None or tab.lead_pane.session is None:
            self._status.showMessage(
                "Lead pane isn't running — open or focus a project tab first.",
                5_000,
            )
            return
        project = tab.project_name
        # Mark fired so first-input handler doesn't re-trigger later.
        self._lead_first_input_fired.add(project)
        self.orch.inject_slash_command_when_ready(LEAD.name, "/remote-control", project=project)
        self._status.showMessage(f"bridging Lead·{project} → claude.ai/code", 4_000)

    def _on_pipelines_clicked(self) -> None:
        """Open the pipeline-settings dialog (drag-drop hops, templates,
        provider/role enable). On Save & Apply the page persists templates +
        per-role enable to `~/.takkub/pipelines.json` via the bridge and stashes
        the desired provider on/off. We then route any *changed* provider
        through `orchestrator.toggle_provider` so it persists to
        `disabled-providers.json`, repaints the status-bar chip, AND broadcasts
        the `[system]` notice to live Lead panes — identical to a chip click.
        Cancel / window-close discards (dialog returns Rejected).
        """
        from .pipeline_dialog import PipelineSettingsDialog
        from .provider_state import is_disabled

        dlg = PipelineSettingsDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        # Apply only providers whose target state differs from disk — toggle_provider
        # always broadcasts, so a no-op call would spam Lead panes spuriously.
        for provider, target_disabled in dlg.bridge.pending_provider_disabled.items():
            if target_disabled != is_disabled(provider):
                self.orch.toggle_provider(provider, target_disabled)
        self._status.showMessage(
            "Pipeline settings saved — applies to the next pane you spawn.",
            6_000,
        )

    def _on_claude_auth_clicked(self) -> None:
        """Open optional Claude auth override settings."""
        from .claude_auth_dialog import ClaudeAuthDialog

        dlg = ClaudeAuthDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self._status.showMessage(
            "Claude auth saved — close and respawn Claude panes to use the new settings.",
            7_000,
        )

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

    def _update_status(self) -> None:
        from .token_meter import effective_context_limit

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
                limit = effective_context_limit(
                    usage["model"], usage["prompt"], base=getattr(p, "_context_limit", None)
                )
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
            peak_limit = 0
            for role_name, pane in panes.items():
                usage = pane.current_usage()
                if not usage:
                    continue
                lim = effective_context_limit(
                    usage["model"], usage["prompt"], base=getattr(pane, "_context_limit", None)
                )
                ratio = (usage["prompt"] / lim) if lim else 0.0
                if ratio > peak_ratio:
                    peak_ratio = ratio
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
                # Tab shows only the % — the absolute count lives on the
                # pane header (the canonical per-pane meter). Avoids the
                # same number appearing in three places (tab/header/status).
                self.tabs.setTabText(i, f"{tab.project_name} · {int(peak_ratio * 100)}%")
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
        #
        # Only meaningful with 2+ active panes: with a single pane the Σ just
        # echoes that pane's own header meter, so we hide it (de-dup) and let
        # the pane header be the single source of truth.
        if len(per_role) >= 2:
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

    def _on_resume_clicked(self) -> None:
        """Send /resume to the active tab's Lead pane so claude opens its
        session picker. No-op if Lead isn't ready yet — slash injection
        helper drops silently in that case (max wait 45s)."""
        active_project = None
        try:
            from .config import active_project as _active_project

            name, _ = _active_project()
            active_project = name
        except Exception:
            pass
        self.orch.inject_slash_command_when_ready("lead", "/resume", project=active_project)

    def _on_end_session_clicked(self) -> None:
        """🏁 End-Session button: prompt for note → close teammates → write summary.

        The note becomes the body of `runtime/sessions/<date>/<project>/lead-*.md`
        and is auto-injected into the next Lead spawn for this project via
        `_recent_session_brief`. So a good note here is what makes the next
        session "remember" today's work.
        """
        try:
            from .config import active_project as _active_project

            project_name, _ = _active_project()
        except Exception:
            project_name = None
        scope = project_name or "active project"

        note, ok = QInputDialog.getMultiLineText(
            self,
            "End Session",
            (
                f"Session-end note for **{scope}**.\n\n"
                "เขียนสั้นๆ ว่า:\n"
                "• เสร็จอะไรไปวันนี้\n"
                "• ค้างอะไรไว้ที่ session หน้าควรหยิบต่อ\n\n"
                "(note นี้จะ auto-inject เข้า Lead's prompt ใน session หน้า)"
            ),
            "session ended",
        )
        if not ok:
            return
        note = note.strip() or "session ended"

        _closed_ok, closed_msg = self.orch.close_all_teammates(project=project_name)
        end_ok, end_msg = self.orch.end_session(project=project_name, note=note)
        _log_event(
            "ui_end_session",
            project=project_name or "",
            closed=closed_msg,
            written=end_msg,
            ok=end_ok,
        )
        if end_ok:
            self._status.showMessage(f"✅ {closed_msg} · {end_msg}", 10_000)
            self._show_end_session_summary(project_name, end_msg, closed_msg)
        else:
            QMessageBox.warning(self, "End Session failed", end_msg)

    def _show_end_session_summary(
        self, project_name: str | None, end_msg: str, closed_msg: str
    ) -> None:
        """Render the just-written `lead-*.md` in a modal so user sees what got logged.

        Status-bar feedback alone is too quiet — vanishes after 10s and the
        user can miss it entirely. This dialog presents the markdown body
        of the session summary plus the close-teammates result line, so
        "did anything happen?" has an unambiguous answer.
        """
        import pathlib
        import re

        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QTextBrowser,
            QVBoxLayout,
        )

        m = re.search(r"written:\s*(.+)$", end_msg.strip())
        if not m:
            return
        rel_path = m.group(1).strip()
        abs_path = pathlib.Path(rel_path)
        if not abs_path.is_absolute():
            abs_path = REPO_ROOT / rel_path
        if not abs_path.is_file():
            return
        try:
            body = abs_path.read_text(encoding="utf-8")
        except OSError:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"🏁 Session ended — {project_name or 'project'}")
        dlg.resize(640, 520)
        layout = QVBoxLayout(dlg)

        browser = QTextBrowser(dlg)
        browser.setMarkdown(body)
        browser.setOpenExternalLinks(False)
        layout.addWidget(browser)

        footer = QLabel(f"📍 {closed_msg}\n📄 {rel_path}", dlg)
        footer.setStyleSheet("color: #6b7280; font-size: 11px; padding: 4px 0;")
        footer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    def _on_ui_review_clicked(self) -> None:
        """🎨 UI Review button: confirm → spawn critic + gemini design-review pair.

        Resolves the project namespace, asks one Cancel/Ok dialog, then calls
        `orch.broadcast_design_review` which assigns parallel tasks. Status
        bar reflects the outcome with the list of spawned roles.
        """
        try:
            from .config import active_project as _active_project

            project_name, _ = _active_project()
        except Exception:
            project_name = None
        scope = project_name or "active project"

        confirm = QMessageBox.question(
            self,
            "Spawn design-review pipeline",
            (
                f"Spawn the design-review duo for **{scope}**?\n\n"
                "• critic reads runtime/exports/<today>/<project>/screenshots/\n"
                "  and writes a proposal to docs/design-review/<date>-<view>.md\n"
                "• gemini cross-checks visual heuristics on the same shots\n\n"
                "Fire this after QA captures screenshots."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        count, roles = self.orch.broadcast_design_review(project=project_name)
        _log_event("ui_design_review", project=project_name or "", count=count, roles=roles)
        if count == 0:
            self._status.showMessage(
                "🎨 Could not spawn design-review panes (check providers / project paths)",
                7_000,
            )
        else:
            self._status.showMessage(f"🎨 Design review pipeline armed: {', '.join(roles)}", 10_000)

    def _on_open_shell_clicked(self) -> None:
        """💻 Shell button: spawn (or focus) a plain PowerShell pane.

        Routes through `orch.spawn('shell', ...)` which hits the non-claude
        shell branch added in orchestrator.spawn(). Re-clicking when the
        pane is already alive surfaces it instead of double-spawning —
        orchestrator returns "already running" and we just nudge focus.
        """
        try:
            from .config import active_project as _active_project

            project_name, _ = _active_project()
        except Exception:
            project_name = None

        ok, msg = self.orch.spawn("shell", project=project_name)
        _log_event("ui_open_shell", project=project_name or "", ok=ok, msg=msg)
        if not ok:
            self._status.showMessage(f"💻 Shell: {msg}", 7_000)
            return

        # Focus the freshly-spawned (or already-running) pane so the
        # user's next keystroke lands in the shell, not the Lead.
        tab = self._tab_for_project(project_name) if project_name else self._current_tab()
        if tab is not None:
            pane = tab.teammate_panes.get("shell")
            if pane is not None:
                pane.setFocus()
                try:
                    pane._terminal.setFocus()
                except Exception:
                    pass
        self._status.showMessage(f"💻 {msg}", 5_000)

    def _on_bug_check_clicked(self) -> None:
        """🐛 Bug-Check button: confirm → broadcast introspection prompt to every pane.

        Each pane's agent decides whether to file a `takkub issue new` or
        send a 'no bugs' note back. No-op if no active panes exist.
        """
        try:
            from .config import active_project as _active_project

            project_name, _ = _active_project()
        except Exception:
            project_name = None
        scope = project_name or "active project"

        confirm = QMessageBox.question(
            self,
            "Broadcast bug check",
            (
                f"Send a bug-introspection prompt to every active pane in **{scope}**?\n\n"
                "Each agent will either file a cockpit issue with\n"
                "`takkub issue new` or report 'no bugs' back to Lead."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        count, roles = self.orch.broadcast_bug_check(project=project_name)
        _log_event("ui_bug_check", project=project_name or "", count=count, roles=roles)
        if count == 0:
            self._status.showMessage("🐛 No active panes to bug-check", 7_000)
        else:
            self._status.showMessage(
                f"🐛 Bug-check sent to {count} pane(s): {', '.join(roles)}", 10_000
            )

    def _on_restart_cockpit_clicked(self) -> None:
        """User-triggered full cockpit restart. Counts in-flight panes,
        asks for confirmation, logs the event, then delegates to
        `_restart_cockpit` which persists state and relaunches."""
        working_count = sum(
            1
            for project_panes in self.orch._panes_by_project.values()
            for pane in project_panes.values()
            if getattr(pane, "state", None) in ("working", "active")
        )

        if working_count > 0:
            body = (
                f"{working_count} pane(s) currently working. "
                "Restart will terminate in-flight tasks. Continue?"
            )
        else:
            body = "Restart cockpit? All panes will be closed and the app will relaunch."

        confirm = QMessageBox.question(
            self,
            "Restart cockpit",
            body,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        _log_event("cockpit_restart", reason="user_action", working_panes=working_count)

        # Do NOT close panes here — _restart_cockpit() persists snapshot while
        # panes are still alive (is_alive=True), then atexit kills them.
        # Closing first would produce an empty snapshot → no teammate restore.
        self._restart_cockpit()

    # ------------------------------------------------------------------
    # Threaded recurring update poll (Layer A)
    # ------------------------------------------------------------------

    def _schedule_update_check(self) -> None:
        """Dispatch an UpdateCheckWorker to the global thread pool.
        Skips silently if a previous worker is still running so we
        never stack up parallel git fetches."""
        if self._update_worker_busy:
            return
        from .update_worker import UpdateCheckWorker

        self._update_worker_busy = True
        worker = UpdateCheckWorker()
        worker.signals.finished.connect(self._on_update_check_done)
        QThreadPool.globalInstance().start(worker)

    def _on_update_check_done(self, status: dict) -> None:
        """Receive result from UpdateCheckWorker, refresh UI, and emit
        a subtle notification when the repo transitions from up-to-date
        to behind — without pestering the user on every subsequent poll."""
        prev = self._update_status_cache or {}
        self._update_status_cache = status
        self._update_worker_busy = False

        # Detect transition: was up-to-date (behind==0, clean, ok),
        # now behind.  Skip notification if previous state was dirty or
        # errored — the tree was already in a non-pristine state.
        prev_up_to_date = (
            prev.get("ok", False) and prev.get("clean", False) and prev.get("behind", 0) == 0
        )
        now_behind = (
            status.get("ok", False) and status.get("clean", False) and status.get("behind", 0) > 0
        )
        if prev_up_to_date and now_behind:
            self._notify_update_available(status["behind"])

        self._refresh_update_button()

    def _notify_update_available(self, n_behind: int) -> None:
        """Flash the update button border and show a system-tray balloon
        when origin/main just gained new commits the user hasn't seen yet.
        Keeps the notification subtle — user can dismiss or ignore."""
        label = f"📦 {n_behind} new commit{'s' if n_behind != 1 else ''} — click to pull"
        self._btn_update.setToolTip(label)

        # Try system tray balloon first (non-modal, disappears on its own).
        tray = getattr(self, "_tray_icon", None)
        if tray is None:
            tray = QSystemTrayIcon(self)
            self._tray_icon = tray
        if tray.isSystemTrayAvailable() and not tray.isVisible():
            tray.setIcon(
                self.windowIcon()
                or self.style().standardIcon(self.style().StandardPixmap.SP_MessageBoxInformation)
            )
            tray.show()
        if tray.isVisible():
            tray.showMessage(
                "Cockpit update available",
                label,
                QSystemTrayIcon.MessageIcon.Information,
                8_000,
            )
        else:
            # Fallback: orange pulsing border on the button (3 flashes).
            self._pulse_update_button(flashes=3)

    def _pulse_update_button(self, flashes: int = 3, _count: int = 0) -> None:
        """Alternate button border between orange and normal 3 times."""
        if _count >= flashes * 2:
            # Restore to whatever _refresh_update_button would set.
            self._refresh_update_button()
            return
        if _count % 2 == 0:
            self._btn_update.setStyleSheet(
                "QPushButton { color: #fde047; background: #422006; "
                "border: 2px solid #f97316; border-radius: 4px; "
                "padding: 2px 8px; font-weight: 600; }"
            )
        else:
            self._btn_update.setStyleSheet(
                "QPushButton { color: #fde047; background: #422006; "
                "border: 1px solid #a16207; border-radius: 4px; "
                "padding: 2px 8px; }"
            )
        QTimer.singleShot(
            400,
            lambda count=_count + 1: self._pulse_update_button(flashes, count),
        )

    # ------------------------------------------------------------------
    # Legacy synchronous check — kept for the click handler's "cache is
    # None" early-boot path only.  All recurring polls now go through
    # _schedule_update_check.
    # ------------------------------------------------------------------

    def _run_update_check(self) -> None:
        """Synchronous fallback used only when the user clicks the chip
        before the first threaded poll has completed (~30 s after boot).
        Runs on the Qt main thread; acceptable for a one-off user action."""
        from .update_helper import fetch_remote, is_git_repo, local_status

        if not is_git_repo():
            self._update_status_cache = {"not_repo": True}
            self._refresh_update_button()
            return
        fetch_remote()  # best effort; ignore failure
        self._update_status_cache = local_status()
        self._refresh_update_button()

    def _refresh_version_label(self) -> None:
        """Update the version chip — live every commit when possible.

        Primary source: `git describe --tags --always --dirty`. Output
        looks like `v0.3.9-3-g4a5b6c7` (3 commits past the v0.3.9 tag)
        or just `4a5b6c7` if no tags exist yet. Adds `-dirty` suffix
        when the working tree has uncommitted changes.

        Fallback: when not in a git checkout (ZIP / wheel install)
        read pyproject.toml and stitch with `@<sha>` if available.
        """
        from .update_helper import current_sha_short, current_version_describe

        described = current_version_describe()
        if described:
            self._version_label.setText(described)
            return
        ver = "?"
        try:
            pyproj = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            import re as _re

            m = _re.search(r'^version\s*=\s*"([^"]+)"', pyproj, _re.MULTILINE)
            if m:
                ver = m.group(1)
        except Exception:
            try:
                from importlib.metadata import version as _pkg_version

                ver = _pkg_version("agent-takkub")
            except Exception:
                pass
        sha = current_sha_short()
        text = f"v{ver} · @{sha}" if sha else f"v{ver}"
        self._version_label.setText(text)

    def _copy_version_to_clipboard(self) -> None:
        text = self._version_label.text().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._status.showMessage(f"copied: {text}", 2000)

    def _show_changelog(self) -> None:
        """Version chip click → render CHANGELOG.md in a scrollable in-app
        dialog (QTextBrowser.setMarkdown — no external browser). The old
        copy-to-clipboard action moves to a button inside the dialog so it
        isn't lost."""
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QPushButton,
            QTextBrowser,
            QVBoxLayout,
        )

        path = REPO_ROOT / "CHANGELOG.md"
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            body = "# Changelog\n\n_ไม่พบ CHANGELOG.md ที่ repo root_"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Changelog · {self._version_label.text().strip() or 'agent-takkub'}")
        dlg.resize(760, 620)
        layout = QVBoxLayout(dlg)

        browser = QTextBrowser(dlg)
        browser.setMarkdown(body)
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet(
            "QTextBrowser { background:#0e0e10; color:#e4e4e7; "
            "border:1px solid #27272a; border-radius:6px; padding:8px; }"
        )
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        copy_btn = QPushButton("📋 Copy version", dlg)
        copy_btn.clicked.connect(self._copy_version_to_clipboard)
        buttons.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        dlg.exec()

    # ------------------------------------------------------------------
    # Claude CLI update (separate from cockpit self-update above)
    # ------------------------------------------------------------------

    def _on_claude_update_clicked(self) -> None:
        """⬆ Claude CLI clicked. Kick a background check (version + changelog +
        AI compatibility analysis); `_on_claude_update_check_done` renders the
        result. Re-entrancy guarded by `_claude_update_busy`."""
        from PyQt6.QtCore import QThreadPool

        from .update_worker import ClaudeUpdateCheckWorker

        if self._claude_update_busy:
            self._status.showMessage("กำลังตรวจ Claude CLI อยู่… รอสักครู่", 4_000)
            return
        self._claude_update_busy = True
        self._btn_claude_update.setEnabled(False)
        self._btn_claude_update.setText("⏳ กำลังตรวจ…")
        self._status.showMessage("ตรวจ Claude CLI + วิเคราะห์ความเข้ากันได้ด้วย AI (อาจใช้เวลาสักครู่)…")
        worker = ClaudeUpdateCheckWorker()
        worker.signals.finished.connect(self._on_claude_update_check_done)
        QThreadPool.globalInstance().start(worker)

    def _on_claude_update_check_done(self, result: dict) -> None:
        """Render the worker result: fatal error → warning; no update → toast;
        update available → report dialog."""
        from PyQt6.QtWidgets import QMessageBox

        self._claude_update_busy = False
        self._btn_claude_update.setEnabled(True)
        self._btn_claude_update.setText("⬆ Claude CLI")
        self._status.clearMessage()

        if not result.get("ok"):
            QMessageBox.warning(
                self, "ตรวจ Claude CLI ไม่สำเร็จ", result.get("error", "unknown error")
            )
            return
        cur = result.get("current") or "?"
        latest = result.get("latest") or "?"
        if not result.get("has_update"):
            QMessageBox.information(
                self,
                "Claude CLI ล่าสุดแล้ว",
                f"ติดตั้งอยู่: v{cur}\nล่าสุดบน npm: v{latest}\n\nไม่ต้องอัพเดต ✅",
            )
            return
        self._show_claude_update_dialog(cur, latest, result)

    def _show_claude_update_dialog(self, cur: str, latest: str, result: dict) -> None:
        """Report dialog: version diff + AI compatibility analysis (markdown),
        with [อัพเดตเลย] / [ปิด] buttons."""
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QLabel,
            QPushButton,
            QTextBrowser,
            QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"อัพเดต Claude CLI · v{cur} → v{latest}")
        dlg.resize(760, 620)
        layout = QVBoxLayout(dlg)

        header = QLabel(f"<b>Claude Code CLI</b>: v{cur} → <b>v{latest}</b>", dlg)
        header.setStyleSheet("color:#e4e4e7; font-size:13px; padding:2px;")
        layout.addWidget(header)

        # Issue auto-filing status (the user wants action-needed findings filed
        # to GitHub so they can fix later). Show what happened.
        issue_line = ""
        if result.get("issue_error"):
            issue_line = f"⚠️ เปิด issue ไม่สำเร็จ: {result['issue_error']}"
            issue_color = "#fca5a5"
        elif result.get("issue_skipped") and result.get("issue_number"):
            issue_line = (
                f"📋 มี issue เดิมสำหรับ version นี้อยู่แล้ว: #{result['issue_number']} "
                f"({result.get('issue_url', '')})"
            )
            issue_color = "#94a3b8"
        elif result.get("issue_number"):
            issue_line = (
                f"📋 เปิด GitHub issue ให้แล้ว: #{result['issue_number']} "
                f"({result.get('issue_url', '')}) — มาสั่งแก้ทีหลังได้"
            )
            issue_color = "#4ade80"
        elif result.get("analysis_ok") and not result.get("issue_action_required"):
            issue_line = "✅ AI ประเมินว่าไม่ต้องแก้ระบบ — ไม่เปิด issue"
            issue_color = "#94a3b8"
        if issue_line:
            issue_label = QLabel(issue_line, dlg)
            issue_label.setWordWrap(True)
            issue_label.setStyleSheet(f"color:{issue_color}; font-size:12px; padding:2px;")
            layout.addWidget(issue_label)

        if result.get("analysis_ok"):
            body = result.get("analysis", "")
        else:
            # Analysis failed (offline / claude error). Don't block the update —
            # just say we couldn't assess and surface why.
            why = result.get("analysis", "ไม่ทราบสาเหตุ")
            body = (
                "## ⚠️ วิเคราะห์ความเข้ากันได้ไม่สำเร็จ\n\n"
                f"`{why}`\n\n"
                "ยังอัพเดตได้ แต่ไม่มีรายงานความเข้ากันได้ — ดู changelog เองที่ "
                "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
            )
            if not result.get("changelog_ok"):
                body += "\n\n_(โหลด changelog ไม่ได้ด้วย — อาจ offline)_"

        browser = QTextBrowser(dlg)
        browser.setMarkdown(body)
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet(
            "QTextBrowser { background:#0e0e10; color:#e4e4e7; "
            "border:1px solid #27272a; border-radius:6px; padding:8px; }"
        )
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        update_btn = QPushButton(f"⬆ อัพเดตเป็น v{latest}", dlg)
        update_btn.setStyleSheet(
            "QPushButton { color:#fff; background:#2563eb; border:none; "
            "border-radius:4px; padding:4px 12px; font-weight:500; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )

        def _do_update() -> None:
            dlg.accept()
            self._confirm_and_apply_claude_update(cur, latest)

        update_btn.clicked.connect(_do_update)
        buttons.addButton(update_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _count_live_claude_panes(self) -> int:
        """How many alive panes are backed by claude.exe (Lead is always; a
        teammate unless remapped/substituted to codex/gemini). These hold the
        binary open and must die before `npm install -g` can replace it on
        Windows."""
        from .provider_config import effective_provider_for

        n = 0
        for project_panes in self.orch._panes_by_project.values():
            for role, pane in project_panes.items():
                sess = getattr(pane, "session", None)
                if sess is not None and getattr(sess, "is_alive", False):
                    if effective_provider_for(role) == "claude":
                        n += 1
        return n

    def _confirm_and_apply_claude_update(self, cur: str, latest: str) -> None:
        """Confirm, then update via a detached script + cockpit restart.

        Why a detached script instead of inline `npm install`: on Windows the
        live Lead + teammate claude processes lock the package files, so the
        install can corrupt the CLI (the exact failure that disabled
        autoupdate). We sidestep it: persist state, spawn a detached updater
        that waits for this process (and its panes) to exit, runs the install
        with nothing holding claude, then relaunches the cockpit.
        """
        import subprocess
        import sys

        from PyQt6.QtWidgets import QMessageBox

        from .claude_update import _npm, build_updater_script

        npm = _npm()
        if not npm:
            QMessageBox.warning(self, "อัพเดตไม่ได้", "หา npm ไม่เจอบน PATH")
            return

        live = self._count_live_claude_panes()
        confirm = QMessageBox.question(
            self,
            "อัพเดต Claude CLI",
            f"จะอัพเดต Claude Code CLI: v{cur} → v{latest}\n\n"
            f"บน Windows ต้องปิด claude pane ที่รันอยู่ ({live} pane รวม Lead) ก่อน "
            "เพื่อเลี่ยง file lock ที่ทำให้ install พัง\n\n"
            "เมื่อกดตกลง ระบบจะ:\n"
            "  1. บันทึก session + ปิด cockpit (panes ปิดทั้งหมด)\n"
            "  2. รัน npm install -g (ตอนไม่มี claude รันอยู่)\n"
            "  3. เปิด cockpit ใหม่อัตโนมัติ\n\n"
            "ดำเนินการ?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        # Persist everything up-front (same as _restart_cockpit) — we quit()
        # right after spawning the updater, can't rely on closeEvent.
        for fn in (
            self._save_window_state,
            self._persist_open_tabs,
            self.orch.write_session_snapshot,
            self.orch.write_resume_briefs,
        ):
            try:
                fn()
            except Exception:
                pass
        try:
            if PORT_FILE.exists():
                PORT_FILE.unlink()
        except Exception:
            pass

        is_win = sys.platform == "win32"
        runtime_dir = REPO_ROOT / "runtime"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        log_path = runtime_dir / "claude_update.log"
        script_path = runtime_dir / ("claude_update.ps1" if is_win else "claude_update.sh")
        script = build_updater_script(
            npm=npm,
            python_exe=sys.executable,
            repo_root=str(REPO_ROOT),
            log_path=str(log_path),
            is_windows=is_win,
        )
        try:
            script_path.write_text(script, encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(self, "อัพเดตไม่ได้", f"เขียน updater script ไม่ได้:\n{e}")
            return

        _log_event("claude_update_start", current=cur, latest=latest, live_panes=live)

        try:
            if is_win:
                import shutil as _shutil

                pwsh = _shutil.which("pwsh") or _shutil.which("powershell") or "powershell"
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so the updater
                # outlives the cockpit we're about to quit. NOT CREATE_NO_WINDOW
                # — Win32 forbids combining it with DETACHED_PROCESS; DETACHED
                # already gives the child no inherited console.
                flags = 0x00000008 | 0x00000200
                subprocess.Popen(
                    [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                    cwd=str(REPO_ROOT),
                    close_fds=True,
                    creationflags=flags,
                )
            else:
                subprocess.Popen(
                    ["sh", str(script_path)],
                    cwd=str(REPO_ROOT),
                    close_fds=True,
                    start_new_session=True,
                )
        except Exception as e:
            QMessageBox.critical(
                self,
                "อัพเดตไม่ได้",
                f"spawn updater ไม่สำเร็จ:\n{e}\n\ncockpit ยังเปิดอยู่ตามเดิม",
            )
            return

        QCoreApplication.quit()

    def _refresh_update_button(self) -> None:
        """Flip the update chip's label/colour based on the cached
        status. Five visual states: not-a-repo, error, clean+up-to-date,
        clean+behind, dirty."""
        # Keep the version chip honest after every poll — pulling new
        # commits or external `git pull` from a terminal both change
        # HEAD and we want the chip to reflect that.
        self._refresh_version_label()
        status = self._update_status_cache or {}
        if status.get("not_repo"):
            self._btn_update.setText("🔧 Enable updates")
            self._btn_update.setToolTip(
                "This install isn't git-tracked. Click to convert it into a\n"
                "git checkout linked to the official repo — enables one-click\n"
                "updates from then on. Your projects.json / runtime/ / .venv/\n"
                "are gitignored and stay safe."
            )
            self._btn_update.setStyleSheet(
                "QPushButton { color: #fde047; background: #422006; "
                "border: 1px solid #a16207; border-radius: 4px; "
                "padding: 2px 8px; }"
                "QPushButton:hover { background: #713f12; }"
            )
            return
        if not status.get("ok"):
            self._btn_update.setText("⚠ Update check failed")
            self._btn_update.setToolTip(
                f"Last error: {status.get('error', 'unknown')}\nRestart cockpit to retry."
            )
            self._btn_update.setStyleSheet(
                "QPushButton { color: #fca5a5; background: #450a0a; "
                "border: 1px solid #7f1d1d; border-radius: 4px; "
                "padding: 2px 8px; }"
            )
            return
        if not status.get("clean"):
            n = len(status.get("dirty_files", []))
            self._btn_update.setText(f"⚠ Local edits ({n})")
            self._btn_update.setToolTip(
                "Tracked files have uncommitted changes. Click to see\n"
                "the list. Pull is blocked until you commit or stash."
            )
            self._btn_update.setStyleSheet(
                "QPushButton { color: #fde047; background: #422006; "
                "border: 1px solid #a16207; border-radius: 4px; "
                "padding: 2px 8px; }"
            )
            return
        behind = status.get("behind", 0)
        if behind == 0:
            self._btn_update.setText("🔄 Up to date")
            self._btn_update.setToolTip("On origin/main. Next check in 5 min.")
            self._btn_update.setStyleSheet(
                "QPushButton { color: #4ade80; background: #052e16; "
                "border: 1px solid #166534; border-radius: 4px; "
                "padding: 2px 8px; }"
                "QPushButton:hover { background: #14532d; }"
            )
        else:
            self._btn_update.setText(f"📦 Update available ({behind})")
            self._btn_update.setToolTip(
                f"origin/main has {behind} new commit{'s' if behind != 1 else ''}. Click to pull."
            )
            self._btn_update.setStyleSheet(
                "QPushButton { color: #1e3a8a; background: #93c5fd; "
                "border: 1px solid #2563eb; border-radius: 4px; "
                "padding: 2px 8px; font-weight: 500; }"
                "QPushButton:hover { background: #bfdbfe; }"
            )

    def _on_update_clicked(self) -> None:
        """User clicked the update chip. Branches by cached status:
        not-a-repo (info), dirty (block + show file list), clean +
        up-to-date (no-op toast), clean + behind (single confirm
        dialog → pull → auto-restart).
        """
        from PyQt6.QtWidgets import QMessageBox

        from .update_helper import pull_updates

        # First poll fires 30 s after boot. If the user clicks during
        # that window the cache is still None — don't render a fake
        # "check failed" dialog. Kick the check off immediately and
        # tell the user to retry in a moment.
        if self._update_status_cache is None:
            self._status.showMessage("Checking for updates… click again in a moment.", 4_000)
            self._run_update_check()
            return
        status = self._update_status_cache
        if status.get("not_repo"):
            from .update_helper import OFFICIAL_REPO_URL, init_git_repo

            convert = QMessageBox.question(
                self,
                "Convert to git checkout?",
                "This install isn't git-tracked, so the cockpit can't pull\n"
                "updates the usual way. Want to convert this folder into a\n"
                f"proper git checkout linked to:\n\n  {OFFICIAL_REPO_URL}\n\n"
                "Safe:  projects.json, runtime/, .venv/, *.log, and AGENTS.md\n"
                "       are gitignored and won't be touched.\n"
                "Lost:  any local edits you made to tracked cockpit files\n"
                "       (README, CLAUDE.md, source code) — they'll be\n"
                "       replaced with the upstream version.\n\n"
                "After conversion the 🔄 update chip will work normally.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if convert != QMessageBox.StandardButton.Ok:
                return
            self._status.showMessage("Converting to git checkout — fetching origin/main…", 0)
            ok, msg = init_git_repo()
            self._status.clearMessage()
            if not ok:
                QMessageBox.critical(self, "Convert failed", msg)
                return
            QMessageBox.information(
                self,
                "Converted",
                f"{msg}\n\nCockpit will restart now to apply the upstream version.",
            )
            self._restart_cockpit()
            return
        if not status.get("ok"):
            QMessageBox.warning(
                self,
                "Update check failed",
                f"Could not read git status:\n{status.get('error', 'unknown')}\n\n"
                "The next 5-minute check will retry automatically.",
            )
            return
        if not status.get("clean"):
            # Cache is up to 5 min stale — user may have just committed
            # in a terminal. Re-check synchronously (local-only, fast)
            # before nagging them about dirty files.
            from .update_helper import local_status

            fresh = local_status()
            self._update_status_cache = {**status, **fresh}
            self._refresh_update_button()
            if fresh.get("ok") and fresh.get("clean"):
                self._status.showMessage("Up to date — chip refreshed.", 4_000)
                return
            files = fresh.get("dirty_files", status.get("dirty_files", []))
            preview = "\n".join(f"  • {f}" for f in files[:20])
            more = "" if len(files) <= 20 else f"\n  …and {len(files) - 20} more"
            QMessageBox.warning(
                self,
                "Local edits block update",
                "Tracked files have uncommitted changes:\n\n"
                f"{preview}{more}\n\n"
                "Commit, stash, or revert these before pulling. The\n"
                "update chip will refresh on the next 5-minute tick.",
            )
            return
        behind = status.get("behind", 0)
        if behind == 0:
            self._status.showMessage("Already up to date", 4_000)
            return
        # Single confirmation: pull + auto-restart. Predict the pip
        # warning up front via the pre-pull diff so the user sees it
        # in the same dialog rather than after the pull lands.
        from .update_helper import pyproject_will_change_on_pull

        pip_warn = (
            "\n\n⚠ pyproject.toml is changing — you'll need to run\n"
            "`pip install -e .` in `.venv` after the restart so the\n"
            "new dependencies take effect."
            if pyproject_will_change_on_pull()
            else ""
        )
        confirm = QMessageBox.question(
            self,
            "Pull update + restart",
            f"Pull {behind} commit{'s' if behind != 1 else ''} from origin/main "
            "and restart cockpit to apply the new version?\n\n"
            "⚠ Cockpit will restart immediately after the pull.\n\n"
            "Your project paths (projects.json), session history\n"
            "(runtime/), and venv are safe — only git-tracked files\n"
            f"are touched.{pip_warn}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        ok, msg = pull_updates()
        if not ok:
            QMessageBox.critical(self, "Pull failed", msg)
            self._run_update_check()  # refresh chip with latest state
            return
        # Pull succeeded — restart immediately. No second confirm; the
        # one dialog above already promised this behaviour.
        self._status.showMessage(f"{msg} — restarting…", 6_000)
        QTimer.singleShot(500, self._restart_cockpit)

    def _restart_cockpit(self) -> None:
        """Spawn a fresh agent-takkub process and quit this one.

        Explicitly persists window state, open tabs, and the session-resume
        snapshot BEFORE spawning the successor so data survives even when
        QCoreApplication.quit() skips closeEvent (e.g. called from a
        non-main-thread context or during a forced restart).
        """
        import subprocess
        import sys

        from ._win_console import SUBPROCESS_NO_WINDOW

        # Persist state up-front — don't rely on closeEvent firing after quit().
        try:
            self._save_window_state()
        except Exception:
            pass
        try:
            self._persist_open_tabs()
        except Exception:
            pass
        try:
            self.orch.write_session_snapshot()
        except Exception:
            pass
        try:
            self.orch.write_resume_briefs()
        except Exception:
            pass
        # Release port file so the successor can reclaim or renumber cleanly.
        try:
            if PORT_FILE.exists():
                PORT_FILE.unlink()
        except Exception:
            pass

        try:
            subprocess.Popen(
                [sys.executable, "-m", "agent_takkub"],
                cwd=str(REPO_ROOT),
                close_fds=True,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except Exception as e:
            # If we can't spawn the successor, don't quit — leave the
            # user in their current session and surface the error.
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self,
                "Restart failed",
                f"Could not launch a new cockpit:\n{e}\n\n"
                "Quit this cockpit manually and run agent-takkub.bat.",
            )
            return
        QCoreApplication.quit()

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

    def _on_provider_chip_clicked(self, provider: str) -> None:
        """Toggle a provider on the orchestrator. Orchestrator persists state,
        broadcasts to all Lead panes, and emits providerStateChanged → we
        update the chip style via _on_provider_state_changed."""
        from .provider_state import is_disabled

        currently_disabled = is_disabled(provider)
        # Flip
        ok, msg = self.orch.toggle_provider(provider, not currently_disabled)
        if not ok:
            self._status.showMessage(f"Toggle failed: {msg}", 4000)

    def _on_provider_state_changed(self, provider: str, disabled: bool) -> None:
        """Repaint the affected chip when provider state flips. Triggered by
        Orchestrator.providerStateChanged so both user click and future
        config-file changes from other sources land here."""
        if provider == "codex" and hasattr(self, "_chip_codex"):
            self._chip_codex.setStyleSheet(self._provider_chip_style("codex", disabled))
            self._chip_codex.setToolTip(
                "Codex: disabled — click to enable"
                if disabled
                else "Codex: enabled — click to disable"
            )
        elif provider == "gemini" and hasattr(self, "_chip_gemini"):
            self._chip_gemini.setStyleSheet(self._provider_chip_style("gemini", disabled))
            self._chip_gemini.setToolTip(
                "Gemini: disabled — click to enable"
                if disabled
                else "Gemini: enabled — click to disable"
            )

    def _on_plan_chip_clicked(self) -> None:
        """Flip the account plan on the orchestrator. It persists state,
        broadcasts to live Lead panes, and emits planTierChanged → we repaint
        the chip via _on_plan_tier_changed."""
        from .plan_tier import MAX, PRO, is_pro

        target = MAX if is_pro() else PRO
        ok, msg = self.orch.set_plan_tier(target)
        if not ok:
            self._status.showMessage(f"Plan switch failed: {msg}", 4000)

    def _on_plan_tier_changed(self, tier: str) -> None:
        """Repaint the plan chip when the tier flips. Triggered by
        Orchestrator.planTierChanged so both user click and any future
        programmatic change land here."""
        if not hasattr(self, "_chip_plan"):
            return
        is_pro = tier == "pro"
        self._chip_plan.setText("Pro" if is_pro else "Max")
        self._chip_plan.setStyleSheet(self._plan_chip_style(is_pro))
        self._chip_plan.setToolTip(self._plan_chip_tooltip(is_pro))

    def _on_add_project_clicked(self) -> None:
        """Show a choice dialog: New project (AI-generated rules) vs Import existing."""
        from PyQt6.QtWidgets import QMessageBox

        msg = QMessageBox(self)
        msg.setWindowTitle("Add project")
        msg.setText("How do you want to add this project?")
        btn_new = msg.addButton(
            "✨ New project (AI-generated rules)", QMessageBox.ButtonRole.AcceptRole
        )
        btn_import = msg.addButton("📂 Import existing", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is btn_new:
            self._new_project_with_rules()
        elif clicked is btn_import:
            self._import_existing_project()
        # Cancel → do nothing

    def _import_existing_project(self) -> None:
        """Original add-project flow: select folder → map paths → save."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QFileDialog,
        )

        dir_path = QFileDialog.getExistingDirectory(self, "Select Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = p.name

        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        self._save_and_open_project(name, p, paths, rules_content=None)

    def _new_project_with_rules(self) -> None:
        """New project flow: select folder → prompt → generate rules → preview/edit → map paths → save."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QFileDialog,
            QMessageBox,
        )

        from .config import load_projects

        dir_path = QFileDialog.getExistingDirectory(self, "Select New Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = p.name

        # Warn if same project name already exists from a different path
        data = load_projects()
        existing = (data.get("projects") or {}).get(name)
        if existing:
            existing_paths = list((existing.get("paths") or {}).values())
            if existing_paths:
                p_posix = p.resolve().as_posix()
                if not any(ep == p_posix or ep.startswith(p_posix + "/") for ep in existing_paths):
                    ans = QMessageBox.question(
                        self,
                        "Duplicate project name",
                        f"A project named '{name}' already exists (different folder).\n"
                        "Continuing will overwrite its configuration. Proceed?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return

        # Step 1: prompt dialog
        prompt_text = self._ask_project_description(name)
        if prompt_text is None:
            return  # user cancelled

        # Step 2: generate rules in background
        rules_content = self._generate_rules_with_ui(prompt_text, name)
        if rules_content is None:
            return  # cancelled or failed

        # Step 3: allow re-generation if needed (loop)
        while True:
            result = self._show_rules_editor_dialog(rules_content, name, allow_regenerate=True)
            if result is None:
                return  # Cancel
            if isinstance(result, str):
                rules_content = result
                break  # Save
            # result is True → Regenerate: ask for new prompt and re-gen
            prompt_text = self._ask_project_description(name, prefill=prompt_text)
            if prompt_text is None:
                return
            rules_content = self._generate_rules_with_ui(prompt_text, name)
            if rules_content is None:
                return

        # Step 4: map paths
        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        # Step 5: handle existing CLAUDE.md in target folder
        if (p / "CLAUDE.md").exists():
            ans = QMessageBox.question(
                self,
                "CLAUDE.md exists",
                f"'{name}/CLAUDE.md' already exists.\nReplace it with the generated rules?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.No:
                rules_content = None  # keep existing, skip write

        self._save_and_open_project(name, p, paths, rules_content=rules_content)

    def _ask_project_description(self, project_name: str, prefill: str = "") -> str | None:
        """Show a multiline prompt dialog. Returns the text or None on cancel."""
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QLabel,
            QPlainTextEdit,
            QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Describe project: {project_name}")
        dlg.resize(500, 260)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("อธิบายระบบนี้ (stack, deploy, constraints, conventions):"))
        txt = QPlainTextEdit(dlg)
        txt.setPlaceholderText(
            "e.g. Next.js 14 frontend + FastAPI backend, deploy to Vercel + Fly.io, "
            "TypeScript strict, ห้ามใช้ any, test coverage ≥80%…"
        )
        if prefill:
            txt.setPlainText(prefill)
        lay.addWidget(txt)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return txt.toPlainText().strip() or None

    def _generate_rules_with_ui(self, prompt: str, project_name: str) -> str | None:
        """Run the generator thread and show a busy dialog.  Returns markdown or None."""
        from PyQt6.QtWidgets import (
            QDialog,
            QLabel,
            QPushButton,
            QVBoxLayout,
        )

        busy = QDialog(self)
        busy.setWindowTitle("Generating project rules…")
        busy.setModal(True)
        busy.resize(360, 120)
        lay = QVBoxLayout(busy)
        lay.addWidget(
            QLabel(f"Running claude headless for '{project_name}'…\nThis may take up to 2 minutes.")
        )
        btn_cancel = QPushButton("Cancel")
        lay.addWidget(btn_cancel)

        thread = _RulesGeneratorThread(prompt, project_name, parent=self)
        result_holder: list[str | None] = [None]
        error_holder: list[str | None] = [None]

        def on_finished(content: str) -> None:
            result_holder[0] = content
            busy.accept()

        def on_failed(msg: str) -> None:
            error_holder[0] = msg
            busy.reject()

        def on_cancel() -> None:
            thread.cancel()
            thread.wait(3000)
            busy.reject()

        thread.rulesReady.connect(on_finished)
        thread.failed.connect(on_failed)
        btn_cancel.clicked.connect(on_cancel)

        self._btn_add_project.setEnabled(False)
        try:
            thread.start()
            busy.exec()
            thread.wait(5000)
            thread.deleteLater()
        finally:
            self._btn_add_project.setEnabled(True)

        if result_holder[0] is not None:
            return result_holder[0]

        if error_holder[0]:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Generation failed", error_holder[0])
        return None

    def _run_map_paths_dialog(self, p: Path) -> dict | None:
        """Show the subdirectory → role-key mapping dialog.

        Returns a dict of {key: posix_path} on accept, or None on cancel.
        """
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )

        from .config import load_projects

        name = p.name
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

        data = load_projects()
        existing_paths = {}
        existing_paths_rev: dict[str, str] = {}
        if "projects" in data and name in data["projects"]:
            existing_paths = data["projects"][name].get("paths", {})
            existing_paths_rev = {v: k for k, v in existing_paths.items()}

        inputs: dict[str, tuple[Path, QLineEdit]] = {}
        try:
            subs = sorted(p.iterdir(), key=lambda x: x.name)
        except PermissionError:
            subs = []
        for sub in subs:
            if sub.is_dir() and not sub.name.startswith("."):
                le = QLineEdit()
                le.setPlaceholderText("key (e.g. web, api)")
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
            return None

        paths: dict[str, str] = {}
        for _sub_name, (sub_path, le) in inputs.items():
            key = le.text().strip()
            if key:
                paths[key] = str(sub_path.resolve().as_posix())

        if not paths:
            paths["main"] = str(p.resolve().as_posix())

        return paths

    def _save_and_open_project(
        self, name: str, p: Path, paths: dict, rules_content: str | None
    ) -> None:
        """Write CLAUDE.md (if rules_content given), save projects.json, open tab."""
        from .config import PROJECTS_JSON, load_projects

        if rules_content is not None:
            from .project_rules import write_project_rules

            write_project_rules(p, rules_content)

        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}

        existing = (data.get("projects") or {}).get(name, {})
        data["projects"][name] = {
            "description": existing.get("description", name),
            "paths": paths,
            "presets": existing.get("presets", []),
        }
        data["active"] = name

        PROJECTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(PROJECTS_JSON, data)

        self._refresh_project_list()
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

    def _on_edit_project_rules_clicked(self, project_name: str | None = None) -> None:
        """Open the rules editor for the given project (defaults to active)."""
        from pathlib import Path

        from PyQt6.QtWidgets import QMessageBox

        from .config import active_project, load_projects
        from .project_rules import read_project_rules, write_project_rules

        if project_name:
            data = load_projects()
            proj = (data.get("projects") or {}).get(project_name, {})
            name = project_name
        else:
            name, proj = active_project()

        if not name or not proj:
            QMessageBox.information(self, "No active project", "No project is currently active.")
            return

        proj_paths = proj.get("paths") or {}
        root_str = proj_paths.get("main") or (next(iter(proj_paths.values()), None))
        if not root_str:
            QMessageBox.information(self, "No paths", f"Project '{name}' has no configured paths.")
            return

        project_root = Path(root_str)
        existing = read_project_rules(project_root)
        content = existing or ""

        while True:
            result = self._show_rules_editor_dialog(content, name, allow_regenerate=True)
            if result is None:
                return  # Cancel
            if result is True:
                # Regenerate: ask for description, then generate
                prompt_text = self._ask_project_description(name)
                if prompt_text is None:
                    return
                new_content = self._generate_rules_with_ui(prompt_text, name)
                if new_content is None:
                    return
                content = new_content
                continue
            # Save
            write_project_rules(project_root, result)
            self._status.showMessage(f"Saved project rules for '{name}'", 4_000)
            return

    def _show_rules_editor_dialog(
        self, content: str, project_name: str, allow_regenerate: bool = False
    ):
        """Editable rules dialog (used by both preview and edit flows).

        Returns str (save), True (regenerate), or None (cancel).
        """
        from PyQt6.QtWidgets import (
            QDialog,
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Project rules — {project_name}/CLAUDE.md")
        dlg.resize(680, 500)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Edit {project_name}/CLAUDE.md:"))
        editor = QPlainTextEdit(dlg)
        editor.setPlainText(content)
        lay.addWidget(editor)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("💾 Save")
        btn_cancel = QPushButton("Cancel")
        outcome: list = [None]

        if allow_regenerate:
            btn_regen = QPushButton("🔄 Regenerate from new prompt")
            btn_row.addWidget(btn_regen)

            def do_regen() -> None:
                outcome[0] = True
                dlg.accept()

            btn_regen.clicked.connect(do_regen)

        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        lay.addLayout(btn_row)

        def do_save() -> None:
            text = editor.toPlainText()
            if not text.strip():
                from PyQt6.QtWidgets import QMessageBox

                QMessageBox.warning(
                    dlg,
                    "Cannot save empty rules",
                    "The editor is empty. Add content or cancel to discard.",
                )
                return
            outcome[0] = text
            dlg.accept()

        btn_save.clicked.connect(do_save)
        btn_cancel.clicked.connect(dlg.reject)

        dlg.exec()
        return outcome[0]

    def _on_tab_bar_context_menu(self, pos) -> None:
        """Right-click on tab bar → context menu for project-level actions."""
        from PyQt6.QtWidgets import QMenu

        bar = self.tabs.tabBar()
        tab_idx = bar.tabAt(pos)
        if tab_idx < 0 or tab_idx == self._plus_tab_index():
            return

        widget = self.tabs.widget(tab_idx)
        if not isinstance(widget, ProjectTab):
            return
        proj_name = widget.project_name

        menu = QMenu(self)
        act_edit = menu.addAction("✏️ Edit project…")
        act_rules = menu.addAction("📋 Edit project rules…")
        chosen = menu.exec(bar.mapToGlobal(pos))
        if chosen is act_edit:
            self._on_edit_project_clicked(proj_name)
        elif chosen is act_rules:
            self._on_edit_project_rules_clicked(proj_name)

    def _on_edit_project_clicked(self, proj_name: str) -> None:
        """Edit an existing project's description and paths in-place (no restart needed)."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QLineEdit,
            QMessageBox,
            QVBoxLayout,
        )

        from .config import PROJECTS_JSON, load_projects

        data = load_projects()
        existing = (data.get("projects") or {}).get(proj_name, {})
        if not existing:
            QMessageBox.warning(self, "Project not found", f"Project '{proj_name}' not found.")
            return

        existing_paths: dict[str, str] = existing.get("paths", {})
        existing_desc: str = existing.get("description", proj_name)
        existing_presets: list = existing.get("presets", [])

        # Infer project root from configured paths
        non_main = {k: v for k, v in existing_paths.items() if k != "main"}
        if non_main:
            p = Path(next(iter(non_main.values()))).parent
        elif "main" in existing_paths:
            p = Path(existing_paths["main"])
        else:
            p = None

        if p is None or not p.exists():
            dir_path = QFileDialog.getExistingDirectory(
                self, f"Select root folder for '{proj_name}'"
            )
            if not dir_path:
                return
            p = Path(dir_path)

        # Step 1: description dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit project: {proj_name}")
        dlg.resize(420, 130)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        desc_edit = QLineEdit(existing_desc)
        form.addRow("Description:", desc_edit)
        lay.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_desc = desc_edit.text().strip() or existing_desc

        # Step 2: paths mapping dialog (pre-fills existing mapping automatically)
        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        # Step 3: validate all configured paths exist on disk
        missing = [v for v in paths.values() if not Path(v).exists()]
        if missing:
            QMessageBox.warning(
                self,
                "Invalid paths",
                "These paths do not exist:\n" + "\n".join(missing),
            )
            return

        # Step 4: write atomically, preserving presets; reload without restart
        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}
        data["projects"][proj_name] = {
            "description": new_desc,
            "paths": paths,
            "presets": existing_presets,
        }

        PROJECTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(PROJECTS_JSON, data)

        self._refresh_project_list()
        self._status.showMessage(f"Updated project '{proj_name}'", 4_000)

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
