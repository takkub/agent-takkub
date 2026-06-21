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
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal
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
    active_project,
    get_open_tabs,
    lead_cwd,
    list_project_names,
    preset_roles_for_active,
    set_active_project,
    set_open_tabs,
)
from .limit_panel import LimitPanelMixin
from .logs_panel import LogsPanel
from .orchestrator import Orchestrator, _log_event
from .project_tab import ProjectTab
from .project_wizard import ProjectWizardMixin
from .roles import DEFAULT_TEAMMATES, LEAD, Role, by_name
from .rtk_helper import is_rtk_installed, rtk_binary_available
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
    MainWindowUpdateMixin, ProjectWizardMixin, UserActionsMixin, LimitPanelMixin, QMainWindow
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
    def _provider_chip_style(provider: str, disabled: bool, not_installed: bool = False) -> str:
        """Outline chip for the codex/gemini toggles.

        Three visual states:
          • available      — brand color (green/blue), bold
          • disabled       — gray + strikethrough (toggled off by user)
          • not_installed  — amber, enabled but CLI absent; Claude substitutes
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
        if not_installed:
            # Amber: enabled in config but CLI not on PATH → Claude will substitute
            return (
                "QPushButton { "
                "background:transparent; color:#d97706; "
                "border:1px solid #d97706; border-radius:10px; "
                "padding:2px 10px; font-weight:500; "
                "}"
                "QPushButton:hover { background:rgba(217,119,6,0.08); }"
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
    def _provider_chip_state(provider: str) -> str:
        """Return the chip's display state: 'available', 'disabled', or 'not_installed'.

        'disabled'      — toggled off in disabled-providers.json (user intent)
        'not_installed' — enabled but CLI binary not found; Claude will substitute
        'available'     — enabled and CLI present
        """
        from .provider_state import is_disabled

        if is_disabled(provider):
            return "disabled"
        if provider == "codex":
            try:
                from .codex_helper import find_codex_executable

                installed = find_codex_executable() is not None
            except Exception:
                import shutil

                installed = shutil.which(provider) is not None
        elif provider == "gemini":
            # The `gemini` role runs on Antigravity's `agy` binary.
            try:
                from .gemini_helper import find_agy_executable

                installed = find_agy_executable() is not None
            except Exception:
                import shutil

                installed = shutil.which("agy") is not None
        else:
            installed = True
        return "available" if installed else "not_installed"

    @staticmethod
    def _provider_chip_tooltip(provider: str, state: str) -> str:
        """Human-readable tooltip for a provider chip given its state."""
        name = provider.capitalize()
        if state == "disabled":
            return f"{name}: disabled — click to enable"
        if state == "not_installed":
            return (
                f"{name}: enabled but not installed — Claude will substitute.\n"
                "Loses model diversity. Install the CLI to fix, or click to disable."
            )
        return f"{name}: enabled — click to disable"

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
        # Membership prevents double-firing across both paths.
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

        # ── limit-status corner widget (top-right of tab bar) ───────────
        _limit_container = QWidget(self)
        _limit_hl = QHBoxLayout(_limit_container)
        # Extra right margin keeps the readout from butting up against the
        # window edge / pane chrome buttons (↓ ▾ ×) sitting just below it.
        # Small top margin (2px) lifts the text clear of the tab-strip top edge
        # without dropping it down toward the tab/content seam.
        _limit_hl.setContentsMargins(6, 2, 14, 0)
        _limit_hl.setSpacing(0)
        self._limit_label = QLabel("—", _limit_container)
        # Top-align the text so it sits high in the tab strip (next to the tab
        # labels) instead of being centred in the taller corner band, which
        # dropped it down onto the tab/content seam and clipped it.
        self._limit_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        _limit_hl.setAlignment(self._limit_label, Qt.AlignmentFlag.AlignTop)
        self._limit_label.setStyleSheet(
            "QLabel { color:#52525b; font-size:11px; "
            "font-variant-numeric:tabular-nums; padding:0 2px; }"
        )
        self._limit_label.setToolTip(
            "Claude usage windows (5h / 7d / 7d-Sonnet)\n"
            "Reflects the User: profile selected for this project.\n"
            "Updates every 5 min."
        )
        _limit_hl.addWidget(self._limit_label)
        self.tabs.setCornerWidget(_limit_container, Qt.Corner.TopRightCorner)
        # Match the corner widget's height to the tab bar so the limit readout
        # lines up flush with the tabs — but never let it be shorter than the
        # label itself needs, or the text gets clipped at the top edge.
        _limit_container.setFixedHeight(
            max(
                self.tabs.tabBar().sizeHint().height(),
                self._limit_label.sizeHint().height(),
            )
        )

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

        # user profile selector moved to ⚙ Pipelines QMenu

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
        from .provider_state import CODEX, GEMINI

        _codex_state = self._provider_chip_state(CODEX)
        self._chip_codex = QPushButton("● Codex", self)
        self._chip_codex.setToolTip(self._provider_chip_tooltip(CODEX, _codex_state))
        self._chip_codex.setStyleSheet(
            self._provider_chip_style(
                CODEX,
                disabled=_codex_state == "disabled",
                not_installed=_codex_state == "not_installed",
            )
        )
        self._chip_codex.clicked.connect(lambda: self._on_provider_chip_clicked(CODEX))

        _gemini_state = self._provider_chip_state(GEMINI)
        self._chip_gemini = QPushButton("● Gemini", self)
        self._chip_gemini.setToolTip(self._provider_chip_tooltip(GEMINI, _gemini_state))
        self._chip_gemini.setStyleSheet(
            self._provider_chip_style(
                GEMINI,
                disabled=_gemini_state == "disabled",
                not_installed=_gemini_state == "not_installed",
            )
        )
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

        # ── Claude CLI check button ────────────────────────────────
        # Separate from _btn_update (which pulls agent-takkub source). Clicking
        # this hands a version-check task to the active tab's Lead pane
        # (_on_claude_update_clicked → orch.inject_lead_prompt) so the Lead
        # reports `claude --version` vs npm in chat and the user decides there.
        # The native worker+dialog self-update flow (_on_claude_update_check_done,
        # _show_claude_update_dialog, _confirm_and_apply_claude_update) is kept
        # below for the close-panes detached path but is no longer wired here.
        self._btn_claude_update = QPushButton("⬆ Claude CLI", self)
        self._btn_claude_update.setToolTip(
            "ยิงคำขอเช็ค Claude Code CLI version เข้า Lead pane\n"
            "Lead จะเทียบ version ที่ติดตั้ง vs ล่าสุดบน npm แล้วรายงานในแชต\n"
            "(แทน native dialog เดิม — user ตัดสินใจอัพเดตจากบทสนทนา)"
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

        # 🩺 Doctor button: runs cockpit environment checks and shows a
        # report with optional one-click auto-fix for fixable findings.
        self._btn_doctor = QPushButton("🩺 Doctor", self)
        self._btn_doctor.setToolTip(
            "Run cockpit environment diagnostics (claude binary, runtime,\n"
            "plugins, MCPs, projects, providers). Shows a report with\n"
            "one-click Fix for auto-fixable findings."
        )
        self._btn_doctor.setStyleSheet(self._ghost_button_style())
        self._btn_doctor.clicked.connect(self._on_doctor_clicked)

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
            "(claude/codex/gemini). Templates + per-role CLI are saved PER PROJECT\n"
            "(~/.takkub/projects/<project>/) so tabs don't collide; provider on/off\n"
            "stays global. Applies to the next pane you spawn, no restart needed."
        )
        self._btn_pipelines.setStyleSheet(self._ghost_button_style())
        self._btn_pipelines.clicked.connect(self._show_pipelines_menu)

        # ▶ Run button removed per user request — it rendered as a stray widget
        # at the window origin (parent=self, never placed in a layout) and
        # covered the first project tab. Pipelines are still fired via the
        # ⚙ Pipelines editor + orch.run_pipeline(id) / CLI.

        self._btn_claude_auth = QPushButton("Claude Auth", self)
        self._btn_claude_auth.setToolTip(
            "Configure optional Claude Code base URL / API key / auth token overrides.\n"
            "Leave fields blank to use Claude Code's default login/session."
        )
        self._btn_claude_auth.setStyleSheet(self._ghost_button_style())
        # Claude Auth now lives as a tab inside the "Add / Remove user…" dialog
        # (_on_claude_auth_clicked was folded in there). This hidden button, if
        # ever restored, opens that combined dialog.
        self._btn_claude_auth.clicked.connect(self._on_add_user_clicked)
        # Hidden per user request (kept for easy future restore). It's created +
        # wired but NOT added to the status bar; hide() so an unplaced child
        # doesn't render as a stray button at the window origin. To restore:
        # delete this hide() line and re-add it to the Group-3 widget tuple below.
        self._btn_claude_auth.hide()

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
            self._version_label,
            self._btn_add_project,
        ):
            self._status.addPermanentWidget(w)
        self._status.addPermanentWidget(self._make_status_separator())
        for w in (
            self._btn_help,
            self._btn_logs,
            self._btn_resume,
            self._btn_open_shell,
            self._btn_bug_check,
            self._btn_doctor,
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
        for p in self.orch.panes.values():
            if p.session is not None and p.session.is_alive:
                active += 1
                if p.state == "working":
                    working += 1

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
