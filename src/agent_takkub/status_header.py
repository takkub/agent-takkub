"""StatusHeaderMixin — status-bar construction + update helpers.

Extracted from ``MainWindow`` (~580 lines).  All widget refs stay on ``self``
(``self._status``, ``self._chip_plan``, …) so every other mixin and
MainWindow method can touch them unchanged.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QStyle,
    QWidget,
)

from . import cockpit_theme

# ── 🧠 Graft chip snapshot caches (MED-6, 2026-08-06 final review) ─────────
# `_graft_progress_snapshot` used to redo `shutil.which()` x2 (a full PATH
# scan) + a `projects.json` walk + a `resolve()`/`is_dir()`/sha256 PER
# CONFIGURED PATH on *every* call — and it was called on the Qt main thread
# every 2s timer tick AND every `orch.statusChanged` emission (reviewer
# measured 14.4ms/call at 46 project dirs; worse, `is_dir()` on a
# disconnected mapped drive blocks for the SMB timeout, on the GUI thread).
# Two module-level caches fix the shape without touching the GUI-thread
# constraint: the CLI path never changes mid-session (resolved once), and
# the rest of the snapshot is cheap enough to redo only every 10s — chip
# granularity doesn't need 2s resolution. `_reset_graft_caches()` is the
# test-only escape hatch so each test's fresh monkeypatching still lands.
_UNSET = object()
_graft_cli_cache: str | object | None = _UNSET
_graft_snapshot_cache: dict | None = None
_graft_snapshot_cache_at: float = 0.0
_GRAFT_SNAPSHOT_TTL_S = 10.0


def _reset_graft_caches() -> None:
    """Test hook: clear both graft-chip caches so the next
    `_graft_progress_snapshot()` call recomputes from scratch instead of
    returning a value cached under a previous test's monkeypatches."""
    global _graft_cli_cache, _graft_snapshot_cache, _graft_snapshot_cache_at
    _graft_cli_cache = _UNSET
    _graft_snapshot_cache = None
    _graft_snapshot_cache_at = 0.0


class StatusHeaderMixin:
    """Mixin for cockpit status-bar construction, styling, and live updates."""

    # ──────────────────────────────────────────────────────────────
    # static style helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_status_separator() -> QWidget:
        """A thin vertical divider between status-bar groups.

        Every chip already draws its own 1px outline, so a bare 1px line
        sitting flush against them used to disappear into the row of
        borders (14+ outlined chips reading as one blob). Wrapping the
        line in a widget with real left/right margin gives the eye a
        visible gap to land on, not just one more thin stroke among many.
        """
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setFixedWidth(1)
        sep.setStyleSheet(
            f"QFrame {{ color: {cockpit_theme.BORDER_STRONG}; "
            f"background: {cockpit_theme.BORDER_STRONG}; }}"
        )
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(0)
        lay.addWidget(sep)
        return wrap

    @staticmethod
    def _plan_chip_label(is_pro: bool) -> str:
        """'Plan: Pro' / 'Plan: Max' — the bare 'Pro'/'Max' text used to sit
        right next to the '👤 1:1' / '👥 Multi' exec-mode chip with a near
        -identical outline style; the two easily got mistaken for the same
        kind of toggle. The 'Plan:' prefix removes the ambiguity without
        needing the reader to memorise a colour legend."""
        return "Plan: Pro" if is_pro else "Plan: Max"

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
    def _overage_chip_style() -> str:
        """Outline chip for the ⚠ usage-overage warning.

        Unlike the other status-bar chips this one is never toggled — it's a
        pure warning, shown only while true — so it has a single style (warn
        amber), not a paired on/off pair like _auto_resume_chip_style.
        """
        brand = cockpit_theme.STATE_WARN_ALT
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; }"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
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
    def _graft_chip_style(state: str) -> str:
        """Outline chip for the 🧠 Graft code-graph auto-build indicator.

        `building` = info blue (quiet background activity, nothing needed
        from the user), `attention` = warn amber (CLI missing or a build
        failed — actionable), `idle` = neutral zinc (nothing happening /
        everything already built — matches the exec-mode/auto-resume chips'
        OFF treatment so it reads as calm, not alarming)."""
        brand = {
            "building": cockpit_theme.STATE_INFO,
            "attention": cockpit_theme.STATE_WARN_ALT,
        }.get(state, cockpit_theme.TEXT_MUTED)
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; "
            f"border:1px solid {brand}; border-radius:{cockpit_theme.RADIUS_MD}px; "
            "padding:2px 10px; font-weight:600; }"
            "QPushButton:hover { background:rgba(255,255,255,0.06); }"
        )

    @staticmethod
    def _performance_chip_style(overloaded: bool) -> str:
        brand = cockpit_theme.STATE_ERROR_BRIGHT if overloaded else cockpit_theme.STATE_OK_BRIGHT
        return (
            "QPushButton { "
            f"background:transparent; color:{brand}; border:1px solid {brand}; "
            f"border-radius:{cockpit_theme.RADIUS_MD}px; padding:2px 10px; font-weight:600; }}"
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

        # ── account plan chip (Pro / Max) ──────────────────────────
        # Records whether the owner is on Pro or Max so the orchestrator can
        # pin the Lead to a standard-context model under Pro (the 1M-context
        # variant is usage-credits gated and hard-errors on Pro). State lives
        # in plan.json; orchestrator owns persist+broadcast on flip.
        from .plan_tier import is_pro as _plan_is_pro

        _pro_now = _plan_is_pro()
        self._chip_plan = QPushButton(self._plan_chip_label(_pro_now), self)
        self._chip_plan.setToolTip(self._plan_chip_tooltip(_pro_now))
        self._chip_plan.setStyleSheet(self._plan_chip_style(_pro_now))
        self._chip_plan.clicked.connect(self._on_plan_chip_clicked)

        # ⚠ Usage-overage chip: warns when the ACTIVE project's Claude
        # account has fully exhausted its 5-hour usage window. Anthropic
        # shortens the prompt-cache TTL (~1h → ~5min) while an account is in
        # this state, so an idle pane recaches its whole transcript far
        # sooner than usual (see limit_status.is_in_overage + the
        # proactive-idle-compact watchdog in orchestrator.py, which is the
        # actual mitigation — this chip is only the visible signal). Reuses
        # the LimitStore cache the tab-corner usage meter already polls
        # (limit_panel.py) — no extra network fetch. Hidden by default;
        # _refresh_overage_chip (called from _update_status, same as the
        # other status chips) shows it only while true.
        self._chip_overage = QPushButton("⚠ Usage overage", self)
        self._chip_overage.setStyleSheet(self._overage_chip_style())
        self._chip_overage.setToolTip(
            "This account's 5-hour usage window is fully exhausted.\n"
            "Claude's prompt-cache TTL shortens (~1h → ~5min) while this "
            "lasts, so idle panes\nrecache their whole context sooner than "
            "usual. Clears when the 5-hour window\nresets (see the usage "
            "meter in the tab corner for the countdown)."
        )
        self._chip_overage.clicked.connect(
            lambda: self._status.showMessage(
                "⚠ Usage overage — 5-hour window exhausted, prompt-cache TTL shortened "
                "(~1h → ~5min) until it resets",
                8_000,
            )
        )
        self._chip_overage.hide()

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

        # 🧠 Graft chip: surfaces the boot-time / tab-switch code-graph
        # auto-build (graft_autobuild.py) that otherwise runs completely
        # silently — a machine with 46 project dirs building in the
        # background with zero UI feedback looked like the cockpit was just
        # slow for no reason (M6). Click opens a plain-language detail
        # dialog; state/text are repainted by _refresh_graft_chip, called
        # here and then every 2s off the existing status timer (piggy-backs
        # on _update_status — polling a couple dozen marker files is cheap,
        # no dedicated timer needed).
        self._chip_graft = QPushButton("🧠 Graft", self)
        self._chip_graft.setStyleSheet(self._graft_chip_style("idle"))
        self._chip_graft.clicked.connect(self._on_graft_chip_clicked)
        self._graft_status_cache: dict | None = None
        self._refresh_graft_chip()

        # Live host/resource health. The compact chip keeps the four numbers
        # needed during fan-out visible; click opens class/queue detail.
        # NOT added to the status-bar row below — MainWindow mounts this
        # widget together with the token/usage meter in the per-tab corner
        # widget instead (see main_window._usage_corner).
        self._chip_performance = QPushButton("SYS CPU — · RAM — · H 0/— · Q 0", self)
        self._chip_performance.setStyleSheet(self._performance_chip_style(False))
        self._chip_performance.clicked.connect(self._show_performance_health_dialog)
        self._performance_status_cache: dict = {}
        self._refresh_performance_health_chip()

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
        # This button's label swings from "🔄 Checking…" to "📦 Update
        # available (12)" to "⚠ Local edits (3)" as the poll state changes —
        # every swing used to reflow the whole status bar and jump every chip
        # to its right. A stable minimum width (sized for the longest common
        # label) absorbs the short-label states; only rare very-long dirty-
        # file counts still grow past it, which is an acceptable trade for a
        # bar that stays put the rest of the time.
        self._btn_update.setMinimumWidth(150)
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
        self._btn_pipelines.setToolTip("Click: open Team & Roles (roster + create a custom role).")
        self._btn_pipelines.setStyleSheet(self._ghost_button_style())
        self._btn_pipelines.clicked.connect(self._on_team_chip_clicked)

        self._btn_provider = QPushButton("🤖 Accounts", self)
        self._btn_provider.setToolTip("Click: switch user account or provider.")
        self._btn_provider.setStyleSheet(self._ghost_button_style())
        # Left-click now opens the quick switch menu
        self._btn_provider.clicked.connect(self._show_pipelines_menu)
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

        # `project_combo` is retained as a hidden widget so legacy code
        # paths that update it (`_refresh_project_list`, `_on_project_changed`)
        # still link cleanly. The tab strip is now the authoritative
        # project switcher — the combo would just duplicate it visually.
        self._project_combo.hide()

        # Status bar is laid out in 2 semantic groups separated by a thin
        # vertical line, and Group 2 is further split into 4 sub-groups —
        # Group 2 used to dump 10 heterogeneous chips (exec mode, providers,
        # session toggles, system actions) in one run with no internal
        # separator, so it read as a single wall even after the top-level
        # grouping landed. Order within each (sub-)group stays stable across
        # cockpit versions so muscle memory survives upgrades.
        #
        #   Group 1 — Workflow actions (buttons that change pane state)
        #   Group 2 — System status    (cockpit-level toggles + updates)
        #     2a. exec      — account plan · usage-overage warning · execution mode
        #     2b. session   — auto-resume · remote · graft build status
        #     2c. system    — rtk install · restart · team · update
        for w in (
            self._btn_open_shell,
            self._chip_tasks,
            self._btn_doctor,
            self._btn_end_session,
        ):
            self._status.addPermanentWidget(w)
        self._status.addPermanentWidget(self._make_status_separator())
        subgroups = (
            (self._chip_plan, self._chip_overage),
            (self._chip_remote, self._chip_graft),
            (
                self._btn_restart,
                self._btn_pipelines,
                self._btn_provider,
                # self._btn_claude_auth,  # hidden per user request — uncomment to restore.
                # The button + its handler are still created above; only its
                # placement in the status bar is removed so it can come back easily.
                self._btn_update,
            ),
        )
        for i, group in enumerate(subgroups):
            for w in group:
                self._status.addPermanentWidget(w)
            if i < len(subgroups) - 1:
                self._status.addPermanentWidget(self._make_status_separator())
        self._refresh_rtk_button()

        # ── signal wiring + refresh timer ──────────────────────────
        self.cli.started.connect(
            lambda port: self._status.showMessage(f"cockpit ready · cli port {port}")
        )
        self.orch.statusChanged.connect(self._update_status)
        self.orch.planTierChanged.connect(self._on_plan_tier_changed)

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

    def _update_provider_chip(self) -> None:
        """Update the provider chip to reflect the current primary provider and account."""
        from . import user_profile
        from .provider_config import effective_provider_for

        try:
            from .config import active_project as _active_project

            proj, _ = _active_project()
        except Exception:
            proj = None

        if not proj:
            self._btn_provider.setText("🤖 Accounts")
            self._btn_provider.setToolTip("Click: switch user account or provider.")
            return

        # Show the provider actually backing Lead. If a configured provider is
        # disabled/missing, this correctly shows the Claude substitution.
        lead_provider = effective_provider_for("lead", proj)
        current_profile = user_profile.profile_for(proj, provider=lead_provider)

        # Determine if other providers also have non-default profiles
        active_others = []
        for p in ["claude", "codex", "gemini"]:
            if p != lead_provider:
                prof = user_profile.profile_for(proj, provider=p)
                if prof != "default":
                    active_others.append(p)

        text = f"🤖 {lead_provider.capitalize()} ({current_profile})"
        if active_others:
            text += f" (+{len(active_others)})"

        self._btn_provider.setText(text)

        # Tooltip clearly shows all logged in providers
        tooltip = f"Click: switch user account or provider.\n\nActive in {proj}:"
        tooltip += f"\n✅ {lead_provider.capitalize()} (Lead): {current_profile}"
        for p in ["claude", "codex", "gemini"]:
            if p != lead_provider:
                prof = user_profile.profile_for(proj, provider=p)
                tooltip += f"\n✅ {p.capitalize()}: {prof}"
        self._btn_provider.setToolTip(tooltip)

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
            for pane in panes.values():
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
            # The cap-crossing status-bar notice lives in one place —
            # orchestrator's edge-triggered session-cap watchdog (see
            # main_window._on_session_cap_notice) — so this loop stays purely
            # passive: it only feeds the always-visible %/color badges below.
            # Sidebar row shows only the % badge — the absolute count lives on
            # the pane header (the canonical per-pane meter). Avoids the same
            # number appearing in three places (sidebar/header/status).
            self.tabs.set_usage(i, peak_ratio if peak_limit else None)
        port = self.cli._server.serverPort() if self.cli._server.isListening() else 0
        bits = [f"cockpit · cli {port}", f"{active} active"]
        if working:
            bits.append(f"{working} working")
        self._status.showMessage("  ·  ".join(bits))
        self._refresh_graft_chip()
        self._refresh_remote_chip()
        self._refresh_overage_chip()
        self._update_provider_chip()
        self._refresh_active_provider_usage()
        self._refresh_performance_health_chip()

    def _active_health_project(self) -> str | None:
        tabs = getattr(self, "tabs", None)
        tab = tabs.currentWidget() if tabs is not None else None
        project = getattr(tab, "project_name", None)
        return project if isinstance(project, str) else None

    def _refresh_performance_health_chip(self) -> None:
        if "_chip_performance" not in self.__dict__:
            return
        getter = getattr(getattr(self, "orch", None), "performance_status", None)
        if not callable(getter):
            return
        try:
            snap = getter(project=self._active_health_project())
        except Exception:
            return
        self._performance_status_cache = snap
        cpu = round(float(snap.get("cpu_percent", 0)))
        ram = round(float(snap.get("available_memory_percent", 0)))
        heavy = int(snap.get("active_heavy_tasks", 0))
        limits = snap.get("resource_limits") or {}
        heavy_limit = int(limits.get("max_heavy_global", 0))
        resource_queue = int(snap.get("queued_resource_tasks", 0))
        spawn_queue = int(snap.get("spawn_queue_depth", 0))
        total_queue = resource_queue + spawn_queue
        overloaded = bool(snap.get("overloaded"))
        state = "OVERLOAD" if overloaded else "OK"
        self._chip_performance.setText(
            f"SYS {state} · CPU {cpu}% · RAM {ram}% · H {heavy}/{heavy_limit or '—'} · Q {total_queue}"
        )
        self._chip_performance.setStyleSheet(self._performance_chip_style(overloaded))
        by_class = snap.get("active_tasks_by_class") or {}
        browser = int(by_class.get("browser", 0))
        browser_limit = int(limits.get("max_browser_global", 0))
        available_gb = float(snap.get("available_memory_bytes", 0)) / (1024**3)
        total_gb = float(snap.get("total_memory_bytes", 0)) / (1024**3)
        writers = snap.get("writer_queues") or {}
        writer_depth = sum(int(row.get("depth", 0)) for row in writers.values())
        stale_drops = sum(int(row.get("stale_dropped", 0)) for row in writers.values())
        queue_full = sum(int(row.get("queue_full", 0)) for row in writers.values())
        # Issue #195 point 3: surface *why* work is waiting, not just a bare
        # count — pulls the resource governor's per-task denial reason
        # (heavy_project_limit, cpu_high, ...) straight from the snapshot.
        waiting_reasons = sorted(
            {
                str(item.get("reason"))
                for item in snap.get("waiting_tasks", [])
                if item.get("reason")
            }
        )
        resource_queue_line = f"Resource queue: {resource_queue}"
        if waiting_reasons:
            resource_queue_line += f" (waiting: {', '.join(waiting_reasons)})"
        self._chip_performance.setToolTip(
            "System Load: " + state + "\n"
            f"CPU: {cpu}%\nAvailable RAM: {ram}% ({available_gb:.1f}/{total_gb:.1f} GiB)\n"
            f"Heavy agents: {heavy}/{heavy_limit or '—'}\n"
            f"Browser agents: {browser}/{browser_limit or '—'}\n"
            f"{resource_queue_line}\nSpawn queue: {spawn_queue}\n"
            f"Writer depth: {writer_depth} · stale drops: {stale_drops} · full: {queue_full}\n"
            f"Duplicate notices prevented: {int(snap.get('duplicate_notices_prevented', 0))}\n"
            f"Main-thread stalls: {int(snap.get('main_thread_stall_count', 0))}\n"
            "Click for the complete live snapshot."
        )

    def _show_performance_health_dialog(self) -> None:
        self._refresh_performance_health_chip()
        snap = self._performance_status_cache
        by_class = snap.get("active_tasks_by_class") or {}
        limits = snap.get("resource_limits") or {}
        writers = snap.get("writer_queues") or {}
        writer_depth = sum(int(row.get("depth", 0)) for row in writers.values())
        stale_drops = sum(int(row.get("stale_dropped", 0)) for row in writers.values())
        queue_full = sum(int(row.get("queue_full", 0)) for row in writers.values())
        available_gb = float(snap.get("available_memory_bytes", 0)) / (1024**3)
        total_gb = float(snap.get("total_memory_bytes", 0)) / (1024**3)
        waiting_reasons = sorted(
            {
                str(item.get("reason"))
                for item in snap.get("waiting_tasks", [])
                if item.get("reason")
            }
        )
        resource_queue_line = f"Resource queue: {int(snap.get('queued_resource_tasks', 0))}"
        if waiting_reasons:
            resource_queue_line += f" (waiting: {', '.join(waiting_reasons)})"
        overloaded = bool(snap.get("overloaded"))
        cpu_val = float(snap.get("cpu_percent", 0))
        ram_val = float(snap.get("available_memory_percent", 0))
        cpu_pause = float(limits.get("cpu_pause_percent", 0))
        cpu_resume = float(limits.get("cpu_resume_percent", 0))
        ram_pause = float(limits.get("min_available_ram_percent", 0))
        ram_resume = float(limits.get("resume_ram_percent", 0))
        # #274: label the overload reason from the governor's own snapshot
        # (overload_reason) instead of a blind "cpu_high" fallback — and for
        # the "neither metric is over ITS pause line right now" case, spell
        # out which resume threshold hasn't been met yet, since the hysteresis
        # requires BOTH cpu<=cpu_resume AND ram>=resume_ram to clear.
        reason_labels = {
            "cpu_high": "CPU above pause threshold",
            "memory_low": "RAM below pause threshold",
            "waiting_resume": "waiting to clear resume thresholds",
        }
        if overloaded:
            reason_label = reason_labels.get(str(snap.get("overload_reason") or ""), "")
            load_line = "System Load: OVERLOAD — new heavy work paused"
            if reason_label:
                load_line += f" ({reason_label})"
        else:
            load_line = "System Load: Normal"
        lines = [
            load_line,
            f"CPU: {cpu_val:.1f}%",
            f"Available RAM: {ram_val:.1f}% ({available_gb:.1f}/{total_gb:.1f} GiB)",
            "Thresholds: CPU pause ≥"
            f"{cpu_pause:.0f}% / resume ≤{cpu_resume:.0f}% · "
            f"RAM pause <{ram_pause:.0f}% / resume ≥{ram_resume:.0f}%",
        ]
        if overloaded:
            still_needed = []
            if cpu_val > cpu_resume:
                still_needed.append(f"CPU ≤{cpu_resume:.0f}% (now {cpu_val:.1f}%)")
            if ram_val < ram_resume:
                still_needed.append(f"RAM ≥{ram_resume:.0f}% (now {ram_val:.1f}%)")
            if still_needed:
                lines.append("Needed to resume: " + " · ".join(still_needed))
        lines += [
            f"Heavy agents: {int(snap.get('active_heavy_tasks', 0))}/{limits.get('max_heavy_global', '—')}",
            f"Browser agents: {int(by_class.get('browser', 0))}/{limits.get('max_browser_global', '—')}",
            f"Builds: {int(by_class.get('build', 0))}/{limits.get('max_build_global', '—')}",
            f"Tests: {int(by_class.get('test', 0))}/{limits.get('max_test_global', '—')}",
            resource_queue_line,
            f"Spawn queue: {int(snap.get('spawn_queue_depth', 0))}",
            f"Fan-out queue: {int(snap.get('fanout_queue_depth', 0))}",
            f"Writer depth: {writer_depth} · stale drops: {stale_drops} · queue full: {queue_full}",
            f"Duplicate notices prevented: {int(snap.get('duplicate_notices_prevented', 0))}",
            f"Main-thread stalls: {int(snap.get('main_thread_stall_count', 0))}",
            f"Processes observed: {int(snap.get('process_count', 0))}",
        ]
        QMessageBox.information(self, "Performance Health", "\n".join(lines))

    # ──────────────────────────────────────────────────────────────
    # ⚠ Usage-overage chip
    # ──────────────────────────────────────────────────────────────

    def _refresh_overage_chip(self) -> None:
        """Repaint the ⚠ overage chip from the LimitStore cache for the
        ACTIVE project tab — never triggers a new fetch (LimitStore's own
        background poller, registered per-tab in limit_panel.py, keeps that
        cache warm on its own schedule).

        Uses the same `__dict__`-membership guard as `_refresh_remote_chip`/
        `_refresh_graft_chip` so tests exercising a bare `MainWindow.__new__()`
        stub (Qt C++ side never constructed) don't hit the sip `hasattr`
        quirk documented there. Silently hides (never raises) when there's no
        store yet — the 3s boot window before `_init_limit_store` runs — or
        no active project.
        """
        if "_chip_overage" not in self.__dict__:
            return
        store = getattr(self, "_limit_store", None)
        if store is None:
            self._chip_overage.hide()
            return
        from . import user_profile
        from .config import active_project
        from .limit_status import is_in_overage

        proj, _ = active_project()
        if not proj:
            self._chip_overage.hide()
            return
        data = store.get(user_profile.config_dir_for(proj))
        self._chip_overage.setVisible(is_in_overage(data))

    # ──────────────────────────────────────────────────────────────
    # 🧠 Graft chip — code-graph auto-build status
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _graft_progress_snapshot() -> dict:
        """Best-effort snapshot of the boot-time / tab-switch graft
        auto-build sweep (`graft_autobuild.py`), built ONLY from public,
        side-effect-free readers — this never imports a private symbol from
        that module (see the M6 task's file-ownership split; backend owns
        it, this is read-only polling from the UI side).

        `{"available": bool, "total": int, "completed": int,
          "eligible_total": int, "building": int|None,
          "failed": list[str]|None, "skipped": list[str]|None}` — `total`/
        `completed` come from `graft_store`'s public `has_completed_build`
        marker (always accurate: it's the same marker the build itself
        writes last, only on success). `building`/`failed`/`skipped` come
        from `graft_autobuild.get_build_status()` — a real, in-flight count
        of threads currently building, a real list of dirs whose last
        attempt genuinely errored, and a real list of dirs intentionally
        skipped (not a git repo — by design, not a failure). `building` is
        `0` (not falsy-None) the moment nothing is building right now —
        callers MUST tell that apart from `None`, which means the getter
        itself is unavailable (older graft_autobuild, or the import/call
        raised) and the chip has no signal at all beyond the completion
        markers above. `skipped` degrades to `None` the same way on an
        older getter that doesn't report it yet — callers must not treat
        `None` as "zero skipped", only as "unknown".

        `eligible_total` is `total` minus however many of those configured
        paths are `skipped` — the denominator the chip/tooltip/dialog show,
        so a non-git-repo path never inflates the "N built" fraction or
        makes the chip look stuck below 100%.

        MED-6 (2026-08-06 final review, R-2 follow-up): this runs on the Qt
        main thread (2s status timer + every `statusChanged`), and per-call
        cost scales with configured project count — a `shutil.which()` PATH
        scan plus a `resolve()`/`is_dir()`/sha256 per path (measured
        14.4ms/call at 46 dirs; an offline mapped drive's `is_dir()` blocks
        for the SMB timeout on the GUI thread). The resolved CLI path and
        the `total`/`completed` project scan are the expensive, slow-moving
        part — cached under `_GRAFT_SNAPSHOT_TTL_S` (10s), since the chip
        doesn't need 2s resolution for those.

        `building`/`failed`/`skipped` are the OPPOSITE: `graft_autobuild.get_build_status()`
        is a real in-flight snapshot, documented as cheap by design (dict/set
        snapshots under one lock — safe to poll every tick), and it is the
        one signal that must never be stale — a cached `0` here would render
        "confirmed nothing building" at boot while 3 builds are actually
        running, exactly the "stale zero looks like confirmed zero"
        conflation `_refresh_graft_chip`'s docstring forbids. So it is
        called fresh on every invocation, cache hit or miss, and merged
        into the (possibly cached) rest of the snapshot. `_reset_graft_caches()`
        clears the cached half for tests.
        """
        global _graft_cli_cache, _graft_snapshot_cache, _graft_snapshot_cache_at

        def _fresh_build_status() -> tuple[int | None, list[str] | None, list[str] | None]:
            # Optional richer status IF graft_autobuild ever grows a public
            # getter — degrades to progress-only when it's absent, which is
            # the case today.
            try:
                from . import graft_autobuild as _gab

                get_status = getattr(_gab, "get_build_status", None)
                if callable(get_status):
                    live = get_status()
                    if isinstance(live, dict):
                        return live.get("building"), live.get("failed"), live.get("skipped")
            except Exception:
                pass
            return None, None, None

        def _eligible_total(total: int, skipped: list[str] | None) -> int:
            return max(total - len(skipped or []), 0)

        now = time.monotonic()
        if (
            _graft_snapshot_cache is not None
            and (now - _graft_snapshot_cache_at) < _GRAFT_SNAPSHOT_TTL_S
        ):
            snap = dict(_graft_snapshot_cache)
            if snap["available"]:
                snap["building"], snap["failed"], snap["skipped"] = _fresh_build_status()
                snap["eligible_total"] = _eligible_total(snap["total"], snap["skipped"])
            return snap

        from . import graft_store

        if _graft_cli_cache is _UNSET:
            _graft_cli_cache = graft_store.graft_cli_path()
        cli = _graft_cli_cache
        if cli is None:
            snap = {
                "available": False,
                "total": 0,
                "completed": 0,
                "eligible_total": 0,
                "building": None,
                "failed": None,
                "skipped": None,
            }
            _graft_snapshot_cache = snap
            _graft_snapshot_cache_at = now
            return snap

        from pathlib import Path

        from .config import load_projects

        try:
            projects = load_projects().get("projects") or {}
        except Exception:
            projects = {}
        seen: dict[str, Path] = {}
        for proj in projects.values():
            if not isinstance(proj, dict):
                continue
            paths = proj.get("paths")
            if not isinstance(paths, dict):
                continue
            for raw in paths.values():
                if not isinstance(raw, str) or not raw.strip():
                    continue
                try:
                    resolved = Path(raw).expanduser().resolve()
                except OSError:
                    continue
                if not resolved.is_dir():
                    continue
                # MED-5 (2026-08-06 final review): dedup key MUST match the
                # store key `graft_store.graph_store_dir` actually uses
                # (`graph_key` -> `_normalize_for_key`, which case-folds on
                # win32 AND darwin). The old `os.name == "nt"`-only fold
                # left macOS case-variant paths ("/Users/x/Proj" vs.
                # "/Users/x/proj") as two distinct dict entries here while
                # `graph_store_dir` folded them to the SAME store dir —
                # `total` overcounts and two concurrent `graft build`s can
                # race into one store. Reusing `graph_key` keeps this
                # dedup permanently in lockstep with the store key instead
                # of hand-rolling a second copy of the casing rule.
                key = graft_store.graph_key(resolved)
                seen[key] = resolved

        total = len(seen)
        completed = sum(
            1
            for d in seen.values()
            if graft_store.has_completed_build(graft_store.graph_store_dir(d))
        )

        building, failed, skipped = _fresh_build_status()

        snap = {
            "available": True,
            "total": total,
            "completed": completed,
            "eligible_total": _eligible_total(total, skipped),
            "building": building,
            "failed": failed,
            "skipped": skipped,
        }
        # Cache only the slow-moving half (available/total/completed) —
        # building/failed/skipped are recomputed fresh on every call above,
        # so the cached copy below is never read for those keys.
        _graft_snapshot_cache = snap
        _graft_snapshot_cache_at = now
        return snap

    def _graft_chip_tooltip(self, snap: dict) -> str:
        if not snap["available"]:
            return (
                "graft CLI not found on PATH — the code-intelligence MCP has\n"
                "no graph to answer from and returns empty results.\n"
                "Run `takkub doctor --fix` to install it, then restart the\n"
                "cockpit. Click for details."
            )
        completed = snap["completed"]
        eligible_total = snap.get("eligible_total", snap["total"])
        building = snap.get("building")
        failed = snap.get("failed") or []
        skipped = snap.get("skipped") or []
        skip_note = f" ({len(skipped)} skipped, not git repos)" if skipped else ""
        if failed:
            return (
                f"{completed}/{eligible_total} project graphs built{skip_note} "
                f"— {len(failed)} failed — click for details."
            )
        if eligible_total and completed < eligible_total:
            if building:
                return (
                    f"Building code-intelligence graphs in the background: "
                    f"{building} in progress, {completed}/{eligible_total} done{skip_note}."
                )
            if building == 0:
                return (
                    f"{completed}/{eligible_total} project graphs built{skip_note} "
                    f"— none building right now — queued for the next tab switch or restart."
                )
            return (
                f"Building code-intelligence graphs in the background: "
                f"{completed}/{eligible_total} done{skip_note}."
            )
        if eligible_total:
            return f"Code-intelligence graphs ready: {eligible_total}/{eligible_total} projects built{skip_note}."
        if skipped:
            return f"No eligible projects to build — {len(skipped)} configured path(s) aren't git repos."
        return "No project paths configured yet — nothing to build."

    def _refresh_graft_chip(self) -> None:
        """Repaint the 🧠 Graft chip from a fresh snapshot. Uses the same
        `__dict__`-membership guard as `_refresh_remote_chip` so tests that
        exercise a `MainWindow.__new__()` stub (Qt C++ side never
        constructed) don't hit the sip `hasattr`/`getattr` quirk documented
        there.

        `building` distinguishes real states: `None` = no getter (unknown,
        falls back to the marker-based progress text below), `0` = getter
        present but nothing building right now (queued, not "building"),
        `>0` = a real in-flight count — these three MUST render differently
        so "don't know" never looks identical to "confirmed zero".

        `skipped` (not-a-git-repo, intentional) is NEVER folded into `failed`
        and never flips the chip to the attention/warn style on its own —
        it isn't something the user needs to act on. Only a real entry in
        `failed` earns the warn color and the singular word "failed"
        anywhere on the chip (2026-08-06 bug report: skipped dirs were
        rendered as "N failed" and scared the user for nothing)."""
        if "_chip_graft" not in self.__dict__:
            return
        snap = self._graft_progress_snapshot()
        self._graft_status_cache = snap
        completed = snap["completed"]
        eligible_total = snap.get("eligible_total", snap["total"])
        building = snap.get("building")
        failed = snap.get("failed") or []
        if not snap["available"]:
            self._chip_graft.setText("🧠 Graft: not installed")
            self._chip_graft.setStyleSheet(self._graft_chip_style("attention"))
        elif failed:
            self._chip_graft.setText(f"🧠 Graft: {len(failed)} failed")
            self._chip_graft.setStyleSheet(self._graft_chip_style("attention"))
        elif building:
            self._chip_graft.setText(
                f"🧠 Building graphs… {building} now · {completed}/{eligible_total}"
            )
            self._chip_graft.setStyleSheet(self._graft_chip_style("building"))
        elif eligible_total and completed < eligible_total:
            if building == 0:
                self._chip_graft.setText(f"🧠 Graft: {completed}/{eligible_total} queued")
                self._chip_graft.setStyleSheet(self._graft_chip_style("idle"))
            else:
                self._chip_graft.setText(f"🧠 Building graphs… {completed}/{eligible_total}")
                self._chip_graft.setStyleSheet(self._graft_chip_style("building"))
        else:
            self._chip_graft.setText("🧠 Graft ready" if eligible_total else "🧠 Graft")
            self._chip_graft.setStyleSheet(self._graft_chip_style("idle"))
        self._chip_graft.setToolTip(self._graft_chip_tooltip(snap))

    def _graft_viewer_target(self):
        """Resolve the graph store to visualize for the CURRENT project
        tab — the first of its configured paths that already has a
        completed build. Returns `None` if there's no active project tab,
        no configured path, or nothing built yet for any of them (2026-08-06:
        the viewer button must reflect whichever project tab is open, never
        a hardcoded path).

        Duck-types on `project_name` rather than `isinstance(tab, ProjectTab)`
        on purpose: importing `project_tab` transitively pulls in
        `QtWebEngineWidgets` (the xterm.js terminal host), which raises if a
        `QCoreApplication` already exists without it pre-imported — a real
        constraint in tests that build a bare Qt widget without going through
        the app's normal boot order. A plain attribute check needs none of
        that and is exactly as correct here (only `ProjectTab` ever carries
        `project_name`)."""
        from pathlib import Path

        from . import graft_store
        from .config import load_projects

        tabs = getattr(self, "tabs", None)
        tab = tabs.currentWidget() if tabs is not None else None
        project_name = getattr(tab, "project_name", None)
        if not isinstance(project_name, str):
            return None
        try:
            projects = load_projects().get("projects") or {}
        except Exception:
            return None
        proj = projects.get(project_name)
        if not isinstance(proj, dict):
            return None
        paths = proj.get("paths")
        if not isinstance(paths, dict):
            return None
        for raw in paths.values():
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except OSError:
                continue
            if not resolved.is_dir():
                continue
            if graft_store.has_completed_build(graft_store.graph_store_dir(resolved)):
                return resolved
        return None

    def _open_graft_viewer(self, target) -> None:
        """Launch (or reuse) a local `graft viz` server for *target* and open
        it in the OS default browser — not a driver-controlled browser, this
        is the same as clicking a link. Reuses the already-running server for
        the same target instead of spawning a second one on every click; a
        different target (switched project tab) replaces it.

        Spawned detached into its own process group/session (see
        `stop_graft_viewer`'s docstring for why — the CLI is a `.cmd`
        wrapper on Windows and `terminate()` alone only kills that wrapper,
        orphaning the real `node` server underneath, confirmed live while
        building this button: the port stayed open and answering requests
        after `terminate()` returned)."""
        import socket
        import subprocess
        import sys
        import webbrowser

        from . import graft_store
        from ._win_console import SUBPROCESS_NO_WINDOW

        proc = self.__dict__.get("_graft_viewer_proc")
        if (
            proc is not None
            and proc.poll() is None
            and self.__dict__.get("_graft_viewer_target_path") == target
        ):
            webbrowser.open(f"http://127.0.0.1:{self._graft_viewer_port}")
            return
        self.stop_graft_viewer()

        cli = graft_store.graft_cli_path()
        if cli is None:
            return
        store = graft_store.graph_store_dir(target)
        staging = graft_store.staging_dir_for(target)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        creationflags = SUBPROCESS_NO_WINDOW
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                [cli, "--dir", str(store), "viz", "--no-open", "-p", str(port), str(staging)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                **popen_kwargs,
            )
        except OSError:
            QMessageBox.warning(
                self,
                "Code-intelligence graph (graft)",
                "Couldn't start the graph viewer — see the cockpit log for details.",
            )
            return

        self._graft_viewer_proc = proc
        self._graft_viewer_port = port
        self._graft_viewer_target_path = target
        webbrowser.open(f"http://127.0.0.1:{port}")

    def stop_graft_viewer(self) -> None:
        """Kill any running `graft viz` server this window spawned, MUST be
        called from `MainWindow.closeEvent` — `graft viz` doesn't exit on
        its own when the cockpit closes.

        Plain `Popen.terminate()` is NOT enough here and was confirmed live
        while building this button: `graft` is installed as a `.cmd` shim on
        Windows, so the tracked PID is `cmd.exe` running that shim, and the
        real `node` server is its CHILD — `terminate()` kills only the shim,
        the server keeps listening and answering requests. `_open_graft_viewer`
        spawns into its own process group (`CREATE_NEW_PROCESS_GROUP`) /
        session (`start_new_session`) precisely so the whole tree can be
        killed from here: `taskkill /T /F` on Windows, `os.killpg` on POSIX.

        Reads `_graft_viewer_proc` via `self.__dict__.get(...)`, not
        `getattr` — same sip quirk `_refresh_graft_chip` guards against:
        `closeEvent` can run against a `MainWindow.__new__()` test stub
        whose Qt C++ side was never constructed, and `getattr` on a truly
        absent attribute there raises `RuntimeError` instead of behaving
        like a normal missing-attribute lookup."""
        import subprocess
        import sys

        proc = self.__dict__.get("_graft_viewer_proc")
        if proc is not None and proc.poll() is None:
            if sys.platform == "win32":
                from ._win_console import SUBPROCESS_NO_WINDOW

                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=SUBPROCESS_NO_WINDOW,
                    check=False,
                )
            else:
                import os
                import signal

                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self._graft_viewer_proc = None

    def _on_graft_chip_clicked(self) -> None:
        """Click-for-detail — the chip itself stays a quiet passive status
        readout (M6: background build must never pop a modal unprompted),
        this dialog only ever appears from an explicit click.

        `skipped` (not-a-git-repo) gets its own section, worded as ordinary
        and expected, never mixed into `failed` or labeled with the word
        "Failed" — and the dialog only turns into a warning (icon + tone)
        when `failed` is genuinely non-empty. A skip-only dialog stays
        informational (2026-08-06 bug report: the old dialog said "Failed:"
        for dirs that were never supposed to build at all)."""
        snap = self._graft_status_cache or self._graft_progress_snapshot()
        if not snap["available"]:
            QMessageBox.warning(
                self,
                "Code-intelligence graph (graft)",
                "The `graft` CLI isn't installed, so teammate panes get\n"
                "empty answers from the code-graph MCP instead of real\n"
                "results.\n\n"
                "Fix: run `takkub doctor --fix` from a terminal, then\n"
                "restart the cockpit.",
            )
            return
        completed = snap["completed"]
        eligible_total = snap.get("eligible_total", snap["total"])
        building = snap.get("building")
        failed = snap.get("failed") or []
        skipped = snap.get("skipped") or []
        lines = [f"{completed}/{eligible_total} project graphs built."]
        if building:
            lines.append(f"{building} building right now.")
        if skipped:
            lines.append("")
            lines.append(f"Skipped ({len(skipped)}, not a git repo — nothing to do):")
            lines.extend(f"  • {name}" for name in skipped)
        is_warning = bool(failed)
        if failed:
            lines.append("")
            lines.append("Failed:")
            lines.extend(f"  • {name}" for name in failed)
            lines.append("")
            lines.append("These hit a real build error. Open a terminal in that")
            lines.append("project and run `graft build <path>` directly to see")
            lines.append("the actual error text (the status bar only keeps a")
            lines.append("pass/fail marker, not the log). A tab switch or")
            lines.append("cockpit restart retries it automatically.")

        # Offer "Open Graph Viewer" only when something for THIS project tab
        # has actually finished building — a viewer with nothing to show is
        # worse than no button at all.
        viewer_target = self._graft_viewer_target()
        if viewer_target is not None:
            # The viewer always opens on its "Context" tab, which is only
            # populated by the optional LLM `--deep` layer we don't run (no
            # API key spend) — so it's permanently empty. No URL param or
            # localStorage key lets us pick a different default tab from
            # here (checked the shipped viewer bundle), so the fix is this
            # heads-up instead of a silent empty-looking page.
            lines.append("")
            lines.append("Viewer opens on the Context tab — that one's empty")
            lines.append("by design (needs the paid --deep layer, off by")
            lines.append("default). Switch to the Code tab to see the graph.")

        box = QMessageBox(self)
        box.setWindowTitle("Code-intelligence graph (graft)")
        box.setText("\n".join(lines))
        box.setIcon(QMessageBox.Icon.Warning if is_warning else QMessageBox.Icon.Information)
        box.addButton(QMessageBox.StandardButton.Close)
        viewer_btn = None
        if viewer_target is not None:
            viewer_btn = box.addButton("Open Graph Viewer", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if viewer_btn is not None and box.clickedButton() is viewer_btn:
            self._open_graft_viewer(viewer_target)

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
        self._refresh_tunnel_indicator()
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

    def _refresh_tunnel_indicator(self) -> None:
        """Repaint the sidebar's top-left tunnel dot (`project_nav.py`)
        from live `RemoteControl` state — deliberately separate from the
        🌐 Remote status-bar chip (bottom of the window): that chip answers
        "is remote control on", this dot answers "is the tunnel itself
        actually up right now". A friend's fresh-install bug once had the
        named tunnel exit right after start with zero surfacing (fixed via
        `tunnel.py`'s `_verify_named_started` + `RemoteControl.tunnel_error`)
        — this reuses that same state rather than tracking a duplicate copy,
        plus `Tunnel.is_alive` for the (rarer) case of a tunnel dying well
        after a clean start, which `tunnel_error` alone can't catch since
        it's only ever set on the initial `start()` failure path.

        Reads `self.__dict__` (not `getattr`/`hasattr`) for the same reason
        `_refresh_remote_chip` does — tests exercise a bare
        `MainWindow.__new__()` stub whose `tabs` may not exist yet.
        """
        tabs = self.__dict__.get("tabs")
        if tabs is None or not hasattr(tabs, "set_tunnel_status"):
            return
        remote = getattr(self, "_remote", None)
        if remote is None:
            tabs.set_tunnel_status("off", "Tunnel: remote control is off.")
            return
        tunnel = getattr(remote, "_tunnel", None)
        if tunnel is not None and getattr(tunnel, "is_alive", False):
            pid = getattr(tunnel, "pid", None)
            pid_note = f" (pid {pid})" if pid else ""
            tabs.set_tunnel_status(
                "running", f"Tunnel: running{pid_note} — reachable from outside this machine."
            )
            return
        error = getattr(remote, "tunnel_error", None)
        if not error and tunnel is not None:
            # Started fine but the process is gone now — the silent-death
            # case. No fresh tunnel_error was ever set for this path (that
            # field only covers the initial start() failure), so fall back
            # to whatever tail output the named-tunnel drain thread kept.
            detail = (getattr(tunnel, "last_output", "") or "").strip()
            error = f"stopped unexpectedly{': ' + detail if detail else ''}"
        if error:
            tabs.set_tunnel_status("error", f"Tunnel: {error}")
            return
        tabs.set_tunnel_status(
            "off", "Tunnel: not running (remote control is on, no tunnel configured/started)."
        )

    def _refresh_rtk_button(self) -> None:
        """Central rtk toggle UI removed — rtk is forced enabled when binary is present."""
        pass
