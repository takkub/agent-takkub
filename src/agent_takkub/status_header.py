"""StatusHeaderMixin — status-bar construction + update helpers.

Extracted from ``MainWindow`` (~580 lines).  All widget refs stay on ``self``
(``self._status``, ``self._chip_codex``, …) so every other mixin and
MainWindow method can touch them unchanged.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QPushButton,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
)

from . import cockpit_theme
from .config import RUNTIME_DIR, lead_cwd
from .rtk_helper import is_rtk_installed, rtk_binary_available


class StatusHeaderMixin:
    """Mixin for cockpit status-bar construction, styling, and live updates."""

    # ──────────────────────────────────────────────────────────────
    # static style helpers
    # ──────────────────────────────────────────────────────────────

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
        sep.setStyleSheet(
            f"QFrame {{ color: {cockpit_theme.BORDER_STRONG}; "
            f"background: {cockpit_theme.BORDER_STRONG}; }}"
        )
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
                f"background:transparent; color:{cockpit_theme.TEXT_MUTED}; "
                f"border:1px solid {cockpit_theme.BORDER_STRONG}; "
                f"border-radius:{cockpit_theme.RADIUS_MD}px; "
                "padding:2px 10px; font-weight:500; "
                "text-decoration: line-through; "
                "}"
                f"QPushButton:hover {{ background:{cockpit_theme.GROUND_SELECT}; "
                f"color:{cockpit_theme.TEXT_SECONDARY}; }}"
            )
        if not_installed:
            # Amber: enabled in config but CLI not on PATH → Claude will substitute
            return (
                "QPushButton { "
                f"background:transparent; color:{cockpit_theme.STATE_WARN}; "
                f"border:1px solid {cockpit_theme.STATE_WARN}; "
                f"border-radius:{cockpit_theme.RADIUS_MD}px; "
                "padding:2px 10px; font-weight:500; "
                "}"
                "QPushButton:hover { background:rgba(217,119,6,0.08); }"
            )
        # Brand colors: codex teal / gemini blue (provider identity tokens)
        brand = (
            cockpit_theme.PROVIDER_CODEX if provider == "codex" else cockpit_theme.PROVIDER_GEMINI
        )
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
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
        brand = cockpit_theme.CHIP_PLAN_MAX if not is_pro else cockpit_theme.STATE_WARN_ALT
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; "
            "}"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _exec_mode_chip_style(is_parallel: bool) -> str:
        """Outline chip for the SOLO/PARALLEL execution-mode toggle. Parallel =
        emerald (active fan-out), Solo = neutral zinc (calm default)."""
        brand = cockpit_theme.CHIP_EXEC_PARALLEL if is_parallel else cockpit_theme.TEXT_MUTED
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; }"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _exec_mode_chip_tooltip(is_parallel: bool) -> str:
        """Tooltip for the execution-mode chip — states the consequence."""
        if is_parallel:
            return (
                "Execution: PARALLEL (multi) — click to switch to 1:1.\n"
                "Lead splits independent features across several instances per\n"
                "role (frontend#1..#K, …), capped to what the machine can run."
            )
        return (
            "Execution: 1:1 (solo) — click to switch to Multi.\n"
            "Multi makes the Lead fan out multiple agents per role for\n"
            "independent features so big work finishes faster."
        )

    @staticmethod
    def _auto_resume_chip_style(enabled: bool) -> str:
        """Outline chip for the 🌙 auto-resume toggle. ON = amber (quietly
        acting on your behalf while you're away), OFF = neutral zinc
        (calm default — matches the exec-mode chip's OFF treatment)."""
        brand = cockpit_theme.STATE_WARN_ALT if enabled else cockpit_theme.TEXT_MUTED
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; }"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _auto_resume_chip_tooltip(enabled: bool) -> str:
        """Tooltip for the auto-resume chip — states the consequence."""
        if enabled:
            return (
                "Auto-resume 🌙: ON — click to turn off.\n"
                "A teammate pane that hits its usage limit while a task is\n"
                "still pending is parked and woken automatically when the\n"
                "window resets (max 3 park/wake cycles per task)."
            )
        return (
            "Auto-resume 🌙: OFF — click to turn on.\n"
            "Usage-limit panes stay notify-only (current behaviour):\n"
            "you get pinged, but nothing auto-resumes."
        )

    @staticmethod
    def _remote_chip_style(enabled: bool) -> str:
        """Outline chip for the 🌐 Remote toggle. ON = teal (server live,
        reachable from outside this machine), OFF = neutral zinc — same
        treatment as the exec-mode/auto-resume chips."""
        brand = cockpit_theme.CHIP_REMOTE_ON if enabled else cockpit_theme.TEXT_MUTED
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; }"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _remote_chip_tooltip(enabled: bool) -> str:
        """Tooltip for the remote chip — states the consequence."""
        if enabled:
            return (
                "Remote control: ON — this cockpit is reachable from your phone.\n"
                "Click to open settings / disable."
            )
        return "Remote control: OFF — click to set up phone pairing (Cloudflare tunnel)."

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
            f"QPushButton {{ color:{cockpit_theme.TEXT_SECONDARY}; background:transparent; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; border-radius:4px; padding:2px 8px; }}"
            f"QPushButton:hover {{ background:{cockpit_theme.GROUND_SELECT}; "
            f"border-color:{cockpit_theme.BORDER_STRONG2}; }}"
            f"QPushButton:checked {{ background:{cockpit_theme.GROUND_SELECT}; "
            f"color:{cockpit_theme.TEXT_PRIMARY_ALT}; border-color:{cockpit_theme.BORDER_STRONG2}; }}"
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
            f"QPushButton {{ color:{cockpit_theme.BANNER_ERROR_TEXT}; background:transparent; "
            f"border:1px solid {cockpit_theme.BANNER_ERROR_BORDER}; border-radius:4px; "
            "padding:2px 8px; }"
            f"QPushButton:hover {{ background:{cockpit_theme.BANNER_ERROR_BG}; "
            f"border-color:{cockpit_theme.STATE_ERROR}; }}"
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

    # ──────────────────────────────────────────────────────────────
    # status-bar construction
    # ──────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        """Create all status-bar widgets and wire up signals + the refresh timer.

        Called from MainWindow.__init__ after the central widget and tabs are
        built.  Widget refs stored as ``self._xxx`` so other methods/mixins
        can reach them without plumbing.
        """
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("starting...")

        self._project_combo = QComboBox(self)
        self._project_combo.setMinimumWidth(140)
        self._project_combo.setToolTip("Active project (used as default cwd)")
        self._refresh_project_list()
        self._project_combo.currentTextChanged.connect(self._on_project_changed)

        # The 📁 add-project button was removed; its function now lives in the
        # "+" new-tab flow (_on_new_tab_clicked) which offers two modes: open an
        # already-configured project, or add a brand-new one (_on_add_project_clicked).

        # user profile selector moved to the 👥 Team chip's right-click QMenu
        # (was ⚙ Pipelines' left-click menu before the A6-redesign)

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
            f"QPushButton {{ color: {cockpit_theme.GOLD_TEXT_ON}; "
            f"background: {cockpit_theme.METER_AMBER}; "
            f"border: 1px solid {cockpit_theme.STATE_WARN}; border-radius: 4px; "
            "padding: 2px 8px; font-size: 12px; }"
            f"QPushButton:hover {{ background: {cockpit_theme.METER_AMBER_LIGHT}; }}"
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

        # Execution-mode chip: SOLO (1:1, default) ↔ PARALLEL (multi). In
        # PARALLEL the Lead decomposes multi-feature requests and fans out
        # several instances per role. State in exec-mode.json; orchestrator owns
        # persist + broadcast on flip.
        from . import exec_mode as _exec_mode

        _parallel_now = _exec_mode.is_parallel()
        self._chip_exec_mode = QPushButton("👥 Multi" if _parallel_now else "👤 1:1", self)
        self._chip_exec_mode.setToolTip(self._exec_mode_chip_tooltip(_parallel_now))
        self._chip_exec_mode.setStyleSheet(self._exec_mode_chip_style(_parallel_now))
        self._chip_exec_mode.clicked.connect(self._on_exec_mode_chip_clicked)

        # Auto-resume chip (🌙): park panes that hit their usage limit while a
        # task is pending and wake them automatically at reset, instead of
        # only notifying. Default OFF. State in autoresume.json; orchestrator
        # owns persist + broadcast on flip (mirrors the exec-mode chip).
        from . import auto_resume as _auto_resume

        _auto_resume_now = _auto_resume.is_enabled()
        self._chip_auto_resume = QPushButton(
            "🌙 Auto-resume" if _auto_resume_now else "🌙 Auto-resume: off", self
        )
        self._chip_auto_resume.setToolTip(self._auto_resume_chip_tooltip(_auto_resume_now))
        self._chip_auto_resume.setStyleSheet(self._auto_resume_chip_style(_auto_resume_now))
        self._chip_auto_resume.clicked.connect(self._on_auto_resume_chip_clicked)

        # 🌐 Remote chip: opens the remote-control (phone pairing) settings
        # dialog. `remote/` is a delete-to-uninstall bolt-on (see
        # remote/__init__.py) — this chip only ever reaches it through
        # importlib.import_module (never a static import, so import-linter's
        # remote-bolt-on-isolation contract stays green and `rm -rf remote/`
        # just hides the chip instead of crashing boot). Text/style/visibility
        # are set by _refresh_remote_chip, called here and again once
        # main_window._boot() knows whether config.json said enabled=true.
        self._chip_remote = QPushButton(self)
        self._chip_remote.clicked.connect(self._on_remote_chip_clicked)
        self._refresh_remote_chip()

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
            f"QPushButton {{ color: {cockpit_theme.TEXT_MUTED}; background: transparent; "
            "border: none; padding: 2px 6px; font-size: 12px; }"
            f"QPushButton:hover {{ color: {cockpit_theme.TEXT_SECONDARY}; }}"
        )
        self._btn_update.clicked.connect(self._on_update_clicked)
        # Cached result from the most recent poll. Populated lazily so
        # the click handler doesn't re-run git just to re-render the
        # same dialog.
        self._update_status_cache: dict | None = None
        # True while a background UpdateCheckWorker is running; prevents
        # queuing a second fetch before the first one completes.
        self._update_worker_busy: bool = False
        # For npm/pip-installed cockpits (no git checkout): the most recent
        # npm-registry version check, so the "Update via npm" chip can flip
        # colour when a newer build is published — parity with the git
        # behind-count. `{"ok", "current", "latest"}` or None before first poll.
        self._npm_update_cache: dict | None = None
        self._npm_check_busy: bool = False

        # The ⬆ Claude CLI button and its _on_claude_update_clicked handler were
        # removed. The native worker+dialog self-update methods
        # (_on_claude_update_check_done, _show_claude_update_dialog,
        # _confirm_and_apply_claude_update) stay in update_panel for the detached
        # close-panes update path; this flag still guards their re-entry.
        self._claude_update_busy: bool = False

        # The "?" help button was removed — F1 still opens the help dialog
        # (_show_help, wired in main_window._install_shortcuts).

        # Sidebar collapse/expand toggle moved into the sidebar footer itself
        # (ProjectNav owns it now, right above "New project"). It used to live
        # here in the status bar.

        self._btn_restart = QPushButton(self)
        self._btn_restart.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._btn_restart.setToolTip("Restart cockpit (kills all panes, relaunches app)")
        self._btn_restart.clicked.connect(self._on_restart_cockpit_clicked)

        # ↻ Resume button removed (2026-07-10, user request): injecting /resume
        # into the Lead pane raced claude's own picker against the
        # /remote-control auto-bridge and kept cancelling. Use claude's native
        # /resume typed directly in the pane instead.

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

        # 🩺 Doctor button: one-stop cockpit readiness check. Runs environment
        # diagnostics AND folds in the recommended dev-team plugin set — the
        # standalone 🧩 Plugins button was merged in here so "am I set up
        # correctly + up to the shared baseline" is a single click. The report
        # dialog carries both a Fix (auto-fixes) and an Install plugins action.
        self._btn_doctor = QPushButton("🩺 Doctor", self)
        self._btn_doctor.setToolTip(
            "Cockpit readiness check in one place: core versions vs the shared\n"
            "baseline (python/node/npx/claude — min & recommended), plugins,\n"
            "MCPs, projects, providers. One-click Fix for auto-fixable findings\n"
            "and Install for missing dev-team plugins."
        )
        self._btn_doctor.setStyleSheet(self._ghost_button_style())
        self._btn_doctor.clicked.connect(self._on_doctor_clicked)

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

        # 📋 Tasks: toggle the right-hand Task List dock (A8) — previously
        # only reachable via the Ctrl+Shift+T shortcut, which nobody
        # discovers on their own (default-hidden, no visible entry point).
        self._chip_tasks = QPushButton("📋 Tasks", self)
        self._chip_tasks.setToolTip("Toggle the Task List dock (Ctrl+Shift+T)")
        self._chip_tasks.setStyleSheet(self._ghost_button_style())
        self._chip_tasks.clicked.connect(lambda: self._on_toggle_tasks(None))

        # 👥 Team chip (A6-redesign, renamed from "⚙ Pipelines"): opens the
        # gold/IBM-Plex SettingsWindow straight to "Providers & Roles" — team
        # roster + guided custom-role create, the thing users actually reach
        # for here (see user_actions._on_team_chip_clicked; the old standalone
        # "🔧 Tools" dialog it used to open was removed 2026-07-10, superseded
        # 100% by this same SettingsWindow). User-profile switch / Add-Remove-user
        # (Claude Auth) used to live in this chip's left-click dropdown alongside
        # "Pipeline Settings…"; that menu item (and the standalone
        # PipelineSettingsDialog it opened) was removed 2026-07-10 — 100%
        # redundant with 👥 Team's own Pipeline Builder / Templates views. The
        # profile-switch section stayed on RIGHT-click so the common case
        # (manage the team) is a single click, not a menu.
        self._btn_pipelines = QPushButton("👥 Team", self)
        self._btn_pipelines.setToolTip(
            "Click: open Team & Roles (roster + create a custom role).\n"
            "Right-click: switch user profile, Add/Remove user."
        )
        self._btn_pipelines.setStyleSheet(self._ghost_button_style())
        self._btn_pipelines.clicked.connect(self._on_team_chip_clicked)
        self._btn_pipelines.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._btn_pipelines.customContextMenuRequested.connect(self._show_pipelines_menu)

        # ▶ Run button removed per user request — it rendered as a stray widget
        # at the window origin (parent=self, never placed in a layout) and
        # covered the first project tab. Pipelines are still fired via the
        # Pipeline Builder / Templates views (👥 Team chip left-click) + orch.run_pipeline(id) / CLI.

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
        # delete this hide() line and re-add it to the Group-2 widget tuple below.
        self._btn_claude_auth.hide()

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

        # Status bar is laid out in 2 semantic groups separated by a thin
        # vertical line. Without grouping, 14+ widgets scan as one long
        # blob and the user has to recall (not recognize) which button
        # does what. Order within each group stays stable across cockpit
        # versions so muscle memory survives upgrades.
        #
        #   Group 1 — Workflow actions (buttons that change pane state)
        #   Group 2 — System status    (cockpit-level toggles + updates)
        for w in (
            self._btn_open_shell,
            self._chip_tasks,
            self._btn_doctor,
            self._btn_end_session,
        ):
            self._status.addPermanentWidget(w)
        self._status.addPermanentWidget(self._make_status_separator())
        for w in (
            self._chip_plan,
            self._chip_exec_mode,
            self._chip_auto_resume,
            self._chip_remote,
            self._chip_codex,
            self._chip_gemini,
            self._btn_install_rtk,
            self._btn_restart,
            self._btn_pipelines,
            # self._btn_claude_auth,  # hidden per user request — uncomment to restore.
            # The button + its handler are still created above; only its
            # placement in the status bar is removed so it can come back easily.
            self._btn_update,
        ):
            self._status.addPermanentWidget(w)
        # Sync rtk button visibility after every permanent widget has been
        # added, so any layout invalidation triggered by show()/hide() lands
        # on a fully-built status bar rather than a half-built one (an
        # earlier mid-loop call kept the button invisible on first paint).
        self._refresh_rtk_button()

        # ── signal wiring + refresh timer ──────────────────────────
        self.cli.started.connect(
            lambda port: self._status.showMessage(f"cockpit ready · cli port {port}")
        )
        self.orch.statusChanged.connect(self._update_status)
        self.orch.providerStateChanged.connect(self._on_provider_state_changed)
        self.orch.planTierChanged.connect(self._on_plan_tier_changed)
        self.orch.execModeChanged.connect(self._on_exec_mode_changed)
        self.orch.autoResumeChanged.connect(self._on_auto_resume_changed)

        # Refresh status bar every 2s so the working/active count tracks the
        # state transitions that don't emit statusChanged (e.g. working→done
        # transitions inside orchestrator._send_when_ready).
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2_000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

    # ──────────────────────────────────────────────────────────────
    # live status updates
    # ──────────────────────────────────────────────────────────────

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
        from .project_tab import ProjectTab

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
            # Sidebar row shows only the % badge — the absolute count lives on
            # the pane header (the canonical per-pane meter). Avoids the same
            # number appearing in three places (sidebar/header/status).
            self.tabs.set_usage(i, peak_ratio if peak_limit else None)
        port = self.cli._server.serverPort() if self.cli._server.isListening() else 0
        bits = [f"cockpit · cli {port}", f"{active} active"]
        if working:
            bits.append(f"{working} working")
        self._status.showMessage("  ·  ".join(bits))

    # ──────────────────────────────────────────────────────────────
    # 🌐 Remote chip visibility + state
    # ──────────────────────────────────────────────────────────────

    def _refresh_remote_chip(self) -> None:
        """Repaint the 🌐 Remote chip from live state, or hide it silently
        if the `remote/` bolt-on isn't importable (deleted / broken install
        — B4/separability). Dynamic import only, per the
        remote-bolt-on-isolation import-linter contract.

        Uses a plain `__dict__` membership check rather than `hasattr` —
        this is called from `_boot()`, which some tests exercise on a
        `MainWindow.__new__()` stub whose Qt C++ side was never
        constructed; `hasattr`/`getattr` on a truly absent attribute in
        that state raises `RuntimeError` instead of behaving like a normal
        missing-attribute check (PyQt6/sip quirk), which `__dict__` access
        alone doesn't trigger.
        """
        if "_chip_remote" not in self.__dict__:
            return
        import importlib

        try:
            importlib.import_module("agent_takkub.remote")
        except Exception:
            self._chip_remote.hide()
            return

        self._chip_remote.show()
        enabled = getattr(self, "_remote", None) is not None
        self._chip_remote.setText("🌐 Remote ●" if enabled else "🌐 Remote")
        self._chip_remote.setStyleSheet(self._remote_chip_style(enabled))
        self._chip_remote.setToolTip(self._remote_chip_tooltip(enabled))

    # ──────────────────────────────────────────────────────────────
    # rtk install button visibility
    # ──────────────────────────────────────────────────────────────

    def _refresh_rtk_button(self) -> None:
        """Show the install button only when the active project's lead_cwd()
        doesn't already carry the rtk hook. Hidden when rtk isn't on PATH or
        no project is active."""
        import time as _t

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
            log = RUNTIME_DIR / "rtk_button.log"
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
