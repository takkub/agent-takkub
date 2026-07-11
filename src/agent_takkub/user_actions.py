"""UserActionsMixin — toolbar/button handlers (refactor round 4, step A).

Extracted from ``MainWindow`` as a mixin. All methods access ``self.*``
attributes (``orch``, ``_status``, ``_btn_pipelines``, ``_chip_codex``,
``_chip_gemini``, ``_chip_plan``, ``_limit_store``, etc.) initialised in
``MainWindow.__init__``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from . import cockpit_theme
from .config import REPO_ROOT, active_project
from .orchestrator import _log_event


class _DoctorThread(QThread):
    """Run doctor checks (and optional auto-fixes) OFF the Qt main thread.

    ``doctor.run_all_checks()`` chains ~9 subprocess probes (claude/node/npx
    ``--version``, plugin + mcp scans) plus a ``git fetch`` (up to ~8 s on a
    slow/unreachable network). Calling it straight from the 🩺 button slot
    blocked the Qt event loop for that whole duration, freezing the entire
    cockpit. The field symptom "cockpit ดับ" was the user force-killing a UI
    that was wedged here — boot.log's main-thread stack pinned it exactly:
    ``_on_doctor_clicked → run_all_checks → check_version → fetch_remote →
    subprocess.communicate``. Doing the work in this thread keeps the UI live.

    Signals
    -------
    ready(object) — emits ``list[Finding]`` when the checks finish (never raises;
                    emits ``[]`` on unexpected error so the dialog can recover).
    """

    ready: pyqtSignal = pyqtSignal(object)

    def __init__(self, apply_fixes_to: object | None = None, parent=None) -> None:
        super().__init__(parent)
        # When set, run_auto_fixes(these) runs before the re-check (Fix button).
        self._apply_fixes_to = apply_fixes_to

    def run(self) -> None:
        from . import doctor as _doctor

        try:
            if self._apply_fixes_to is not None:
                _doctor.run_auto_fixes(self._apply_fixes_to)
            findings = _doctor.run_all_checks()
        except Exception:
            findings = []
        self.ready.emit(findings)


class _PluginInstallThread(QThread):
    """Install a list of recommended plugins OFF the Qt main thread.

    Each install is a ``claude plugin marketplace add`` + ``claude plugin
    install`` (git clone + network) — bounded by per-call timeouts but still
    multi-second, so it must not run on the Qt event loop. Emits ``progress``
    before each plugin and ``done`` with the per-plugin results.

    Signals
    -------
    progress(str)  — human label of the plugin about to install
    done(object)   — list of (RecommendedPlugin, ok: bool, message: str)
    """

    progress: pyqtSignal = pyqtSignal(str)
    done: pyqtSignal = pyqtSignal(object)

    def __init__(self, plugins: list, parent=None) -> None:
        super().__init__(parent)
        self._plugins = plugins

    def run(self) -> None:
        from . import plugin_installer

        results: list = []
        # Add each shared marketplace once up front (claude-plugins-official
        # backs 4 of the 5), then install each plugin without re-adding it. Keep
        # the per-repo result so a failed marketplace add surfaces as ONE clear
        # "marketplace unavailable" per plugin instead of a cryptic install error.
        mp_results = plugin_installer.ensure_marketplaces(self._plugins)
        for p in self._plugins:
            self.progress.emit(p.label)
            mp_ok, mp_msg = mp_results.get(p.marketplace_repo, (True, ""))
            if not mp_ok:
                results.append((p, False, f"marketplace unavailable ({mp_msg})"))
                continue
            try:
                ok, msg = plugin_installer.install_plugin(p, ensure_marketplace=False)
            except Exception as e:  # pragma: no cover - defensive
                ok, msg = False, str(e)
            results.append((p, ok, msg))
        self.done.emit(results)


# Background workers (_DoctorThread / _PluginInstallThread) are held HERE at
# module level, NOT parented to the window. A worker parented to the window gets
# destroyed when the user closes the cockpit mid-run, aborting with "QThread:
# Destroyed while thread is still running" (a hard crash) whenever a check/install
# outlives a short close-time wait. Keeping the only strong ref here means window
# teardown never deletes a running thread; each worker removes itself on
# `finished` (then deleteLater), so the sets stay small.
_DOCTOR_THREADS: set = set()
_PLUGIN_THREADS: set = set()

# 🌐 Remote quick-tunnel mode: how long _apply_remote_config blocks the
# Enable click waiting for cloudflared to print its *.trycloudflare.com URL.
_QUICK_TUNNEL_WAIT_S = 6.0
_QUICK_TUNNEL_POLL_S = 0.2


class UserActionsMixin:
    """Mixin for cockpit toolbar / status-bar button handlers."""

    # ──────────────────────────────────────────────────────────────
    # user-profile menu
    # ──────────────────────────────────────────────────────────────

    def _show_pipelines_menu(self, *_args) -> None:
        """Show the user-profile drop-down menu.

        A6-redesign moved this off the 👥 Team chip's left-click (now
        ``_on_team_chip_clicked`` — straight to Team & Roles) onto its
        RIGHT-click (``customContextMenuRequested``, which passes a QPoint
        this method ignores in favor of anchoring off the button's own rect).
        Built fresh on every click so user-profile state is always current.
        "Pipeline Settings…" used to be the first section here, opening
        :class:`pipeline_dialog.PipelineSettingsDialog`; removed 2026-07-10 —
        100% redundant with 👥 Team's own Pipeline Builder / Templates views
        (see settings_window.py). Menu sections:
          1. User profiles (checkable, one per profile)
          ─────────────────
          2. Add / Remove user… (now includes Claude Auth tab)
        """
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu

        from . import user_profile

        try:
            from .config import active_project as _active_project

            _proj, _ = _active_project()
        except Exception:
            _proj = None

        menu = QMenu(self)

        current_profile = user_profile.profile_for(_proj or "")
        for profile in user_profile.list_profiles():
            name = profile["name"]
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(name == current_profile)
            act.triggered.connect(lambda _checked, n=name: self._on_user_changed(n))
            menu.addAction(act)

        menu.addSeparator()

        act_manage = QAction("Add / Remove user…", self)
        act_manage.triggered.connect(self._on_add_user_clicked)
        menu.addAction(act_manage)

        menu.exec(self._btn_pipelines.mapToGlobal(self._btn_pipelines.rect().bottomLeft()))

    # ──────────────────────────────────────────────────────────────
    # session control
    # ──────────────────────────────────────────────────────────────

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
        footer.setStyleSheet(
            f"color: {cockpit_theme.TEXT_FAINT_ALT}; font-size: 11px; padding: 4px 0;"
        )
        footer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    # ──────────────────────────────────────────────────────────────
    # broadcast actions (UI review, shell, bug check, doctor)
    # ──────────────────────────────────────────────────────────────

    def _on_team_chip_clicked(self) -> None:
        """👥 Team chip: open the gold/IBM-Plex Settings window straight to
        "Providers & Roles" (design system, 2026-07-10 — fully supersedes the
        old standalone "🔧 Tools" PaneToolsDialog, removed the same day; this
        SettingsWindow is now the single place for MCP/Plugins/Team&Roles
        editing, see settings_window.py's module docstring).
        Right-click on the same chip still reaches user profile switch /
        Add-Remove-user via ``_show_pipelines_menu`` ("Pipeline Settings…"
        used to be the first section there — removed 2026-07-10, redundant
        with this same window's Pipeline Builder / Templates views)."""
        from .settings_window import VIEW_PROVIDERS_ROLES

        self._open_settings_window(VIEW_PROVIDERS_ROLES)

    def _open_settings_window(self, initial_view: int) -> None:
        """Open the gold/IBM-Plex Settings window at *initial_view*, then
        apply any staged provider on/off toggle exactly like a status-bar
        chip click would — shared by every entry point that can land on this
        window (👥 Team chip → Providers & Roles, "Add / Remove user…" menu
        entry → Users tab) so a provider toggled from a non-default landing
        view (e.g. the user navigates away from Users to Providers & Roles
        mid-visit) still gets applied on Save & Apply."""
        from .provider_state import is_disabled
        from .settings_window import SettingsWindow

        try:
            from .config import active_project as _active_project

            _proj, _ = _active_project()
        except Exception:
            _proj = None

        dlg = SettingsWindow(self, project=_proj, initial_view=initial_view)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        # Same apply pattern as _open_pipeline_settings_dialog: only route
        # providers whose target state differs from disk through
        # orchestrator.toggle_provider (it always broadcasts, so a no-op call
        # would spam Lead panes spuriously).
        errors: list[str] = []
        for provider, target_disabled in dlg.pending_provider_disabled.items():
            if target_disabled != is_disabled(provider):
                ok, msg = self.orch.toggle_provider(provider, target_disabled)
                if not ok:
                    errors.append(msg)
        if errors:
            self._status.showMessage("Provider toggle ไม่สำเร็จบางส่วน: " + "; ".join(errors), 8_000)

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

    def _on_doctor_clicked(self) -> None:
        """🩺 Doctor button: run environment checks and display a report dialog.

        The checks (`doctor.run_all_checks()`) run in a background `_DoctorThread`
        so the multi-second `git fetch` + subprocess probes never block the Qt
        main thread — see `_DoctorThread` for why that freeze used to read as the
        cockpit "dying". The dialog opens immediately showing a "running" state
        and is populated when the worker emits its findings. A Fix button appears
        when at least one finding has auto_fix set; clicking it runs the fixes +
        re-check on the same background thread.
        """
        from . import doctor as _doctor

        self._status.showMessage("🩺 Running diagnostics…", 2_000)

        dlg = QDialog(self)
        dlg.setWindowTitle("🩺 Cockpit Doctor")
        dlg.setMinimumSize(560, 480)
        dlg.resize(640, 540)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        report_view = QPlainTextEdit(dlg)
        report_view.setReadOnly(True)
        report_view.setFont(dlg.font())
        _mono = cockpit_theme.ensure_fonts_loaded()["mono"]
        report_view.setStyleSheet(
            f"QPlainTextEdit {{ background:{cockpit_theme.GROUND_PANEL}; "
            f"color:{cockpit_theme.TEXT_SECONDARY}; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; border-radius:4px; padding:6px; "
            f'font-family: "{_mono}"; font-size:12px; }}'
        )
        report_view.setPlainText(
            "🩺 Running diagnostics…\n\n"
            "  checking claude / node / npx / plugins / mcps / projects /\n"
            "  providers / hooks / ready-markers / git version…\n\n"
            "  (running in the background — the cockpit stays responsive)"
        )
        layout.addWidget(report_view, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_fix = QPushButton("Fix", dlg)
        btn_fix.setEnabled(False)
        btn_fix.setToolTip("Running checks…")
        # Primary action → gold (was a green fill — the design system has one
        # primary accent, gold).
        btn_fix.setStyleSheet(
            f"QPushButton {{ background:{cockpit_theme.GOLD_GRAD_BOTTOM}; "
            f"color:{cockpit_theme.GOLD_TEXT_ON}; border:none; "
            "border-radius:5px; padding:4px 14px; font-weight:700; }"
            f"QPushButton:hover {{ background:{cockpit_theme.GOLD_GRAD_TOP}; }}"
            f"QPushButton:disabled {{ background:{cockpit_theme.GROUND_SELECT}; "
            f"color:{cockpit_theme.TEXT_FAINT}; }}"
        )

        # 🧩 Install plugins — the old standalone Plugins button folded into
        # Doctor. Installs any missing recommended dev-team plugins on the same
        # background-thread pattern as everything else here (a `claude plugin
        # install` git-clone must never touch the Qt event loop), then re-runs
        # the checks so the [plugins] findings refresh in place.
        btn_plugins = QPushButton("Install plugins", dlg)
        # Secondary action → bordered secondary treatment (was a blue #2563eb
        # fill competing with the primary button).
        btn_plugins.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{cockpit_theme.TEXT_SECONDARY}; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; "
            "border-radius:5px; padding:4px 14px; font-weight:600; }"
            f"QPushButton:hover {{ background:rgba(255,255,255,0.05); "
            f"color:{cockpit_theme.TEXT_PRIMARY}; }}"
            f"QPushButton:disabled {{ background:{cockpit_theme.GROUND_SELECT}; "
            f"color:{cockpit_theme.TEXT_FAINT}; }}"
        )

        def _refresh_plugins_btn() -> None:
            """Label the plugins button with the live missing-count (disk scan,
            main-thread-safe). No missing → disabled 'Plugins ✓'."""
            from . import plugin_installer

            try:
                missing = plugin_installer.missing_plugins()
            except Exception:
                missing = []
            if missing:
                btn_plugins.setEnabled(True)
                btn_plugins.setText(f"Install plugins ({len(missing)})")
                btn_plugins.setToolTip(
                    "Install the missing recommended dev-team plugins:\n  "
                    + "\n  ".join(p.label for p in missing)
                    + "\n(restart cockpit for panes to pick them up)"
                )
            else:
                btn_plugins.setEnabled(False)
                btn_plugins.setText("Plugins ✓")
                btn_plugins.setToolTip("All recommended dev-team plugins are installed.")

        # Per-plugin install results, shown inside the dialog. The status-bar
        # toast alone proved invisible in the field: the doctor dialog covers
        # the main window's status bar, so a user who clicks Install sees only
        # a grayed button for the 15s–2min git clone and reads it as "dead"
        # (then closes the app, killing the install mid-clone).
        plugin_notes: list[str] = []

        def _install_plugins() -> None:
            from . import plugin_installer

            try:
                missing = plugin_installer.missing_plugins()
            except Exception:
                missing = []
            if not missing:
                _refresh_plugins_btn()
                return
            btn_plugins.setEnabled(False)
            btn_plugins.setText("Installing…")
            report_view.setPlainText(
                "🧩 Installing recommended plugins…\n\n  "
                + "\n  ".join(f"· {p.label}" for p in missing)
                + "\n\n  (git clone + network — may take 1–2 minutes;\n"
                "   keep this dialog open until results appear)"
            )
            worker = _PluginInstallThread(missing)  # no parent — see _PLUGIN_THREADS
            _PLUGIN_THREADS.add(worker)

            def _prog(label: str) -> None:
                try:
                    self._status.showMessage(f"🧩 installing {label}…", 4_000)
                    report_view.appendPlainText(f"\n  → installing {label}…")
                except RuntimeError:
                    pass

            def _plugins_done(results: object) -> None:
                try:
                    ok_n = sum(1 for _p, ok, _m in results if ok)
                    plugin_notes.clear()
                    plugin_notes.extend(
                        f"  {'✓' if ok else '✗'} {p.label} — {msg}" for p, ok, msg in results
                    )
                    _log_event("ui_plugin_install", ok=ok_n, total=len(results))
                    self._status.showMessage(f"🧩 Plugins: {ok_n}/{len(results)} installed", 6_000)
                    report_view.setPlainText(
                        f"🧩 Plugin install finished — {ok_n}/{len(results)} ok\n\n"
                        + "\n".join(plugin_notes)
                        + "\n\n🩺 Re-running diagnostics…"
                    )
                    _refresh_plugins_btn()
                    _start()  # re-run diagnostics so [plugins] findings refresh
                except RuntimeError:
                    pass

            worker.progress.connect(_prog)
            worker.done.connect(_plugins_done)
            worker.finished.connect(lambda w=worker: _PLUGIN_THREADS.discard(w))
            worker.finished.connect(worker.deleteLater)
            worker.start()

        btn_plugins.clicked.connect(_install_plugins)

        # Track the running worker so it isn't garbage-collected mid-run and so a
        # Workers are held in a MODULE-LEVEL set (not parented to the window) so
        # closing the cockpit while a check runs can't destroy a live QThread —
        # see _DOCTOR_THREADS. Each removes itself on `finished`.
        def _start(apply_fixes_to=None) -> None:
            btn_fix.setEnabled(False)
            btn_fix.setToolTip("Running checks…")
            worker = _DoctorThread(apply_fixes_to=apply_fixes_to)
            _DOCTOR_THREADS.add(worker)

            def _on_ready(findings: object) -> None:
                # The dialog may have been closed while the worker ran; guard the
                # C++-object access so a late signal can't raise in the slot.
                try:
                    report_view.setPlainText(_doctor.format_report(findings))
                    if plugin_notes:
                        report_view.appendPlainText(
                            "\n[plugin install — last run]\n" + "\n".join(plugin_notes)
                        )
                    has_fixes = any(f.auto_fix is not None for f in findings)
                    btn_fix.setEnabled(has_fixes)
                    btn_fix.setToolTip(
                        "Run auto-fixes for all fixable findings."
                        if has_fixes
                        else "No auto-fixable findings in this report."
                    )
                    # Rebind Fix to operate on the freshest findings.
                    try:
                        btn_fix.clicked.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                    btn_fix.clicked.connect(lambda: _start(apply_fixes_to=findings))
                    if apply_fixes_to is not None:
                        self._status.showMessage("🩺 Auto-fixes applied", 4_000)
                except RuntimeError:
                    pass

            worker.ready.connect(_on_ready)
            worker.finished.connect(lambda w=worker: _DOCTOR_THREADS.discard(w))
            worker.finished.connect(worker.deleteLater)
            worker.start()

        _start()
        _refresh_plugins_btn()

        btn_close = QPushButton("Close", dlg)
        btn_close.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{cockpit_theme.TEXT_MUTED}; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; border-radius:5px; "
            "padding:4px 14px; }"
            f"QPushButton:hover {{ background:{cockpit_theme.GROUND_SELECT}; "
            f"color:{cockpit_theme.TEXT_SECONDARY}; }}"
        )
        btn_close.clicked.connect(dlg.accept)

        btn_row.addStretch()
        btn_row.addWidget(btn_plugins)
        btn_row.addWidget(btn_fix)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        _log_event("ui_doctor_opened")
        dlg.exec()

    # ──────────────────────────────────────────────────────────────
    # provider / plan chip handlers
    # ──────────────────────────────────────────────────────────────

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
        state = self._provider_chip_state(provider)
        if provider == "codex" and hasattr(self, "_chip_codex"):
            self._chip_codex.setStyleSheet(
                self._provider_chip_style(
                    "codex",
                    disabled=state == "disabled",
                    not_installed=state == "not_installed",
                )
            )
            self._chip_codex.setToolTip(self._provider_chip_tooltip("codex", state))
        elif provider == "gemini" and hasattr(self, "_chip_gemini"):
            self._chip_gemini.setStyleSheet(
                self._provider_chip_style(
                    "gemini",
                    disabled=state == "disabled",
                    not_installed=state == "not_installed",
                )
            )
            self._chip_gemini.setToolTip(self._provider_chip_tooltip("gemini", state))

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

    def _on_exec_mode_chip_clicked(self) -> None:
        """Flip SOLO ↔ PARALLEL on the orchestrator. It persists state,
        broadcasts to live Lead panes, and emits execModeChanged → we repaint
        the chip via _on_exec_mode_changed."""
        from . import exec_mode

        target = exec_mode.SOLO if exec_mode.is_parallel() else exec_mode.PARALLEL
        ok, msg = self.orch.set_exec_mode(target)
        if not ok:
            self._status.showMessage(f"Mode switch failed: {msg}", 4000)
        elif target == exec_mode.PARALLEL:
            self._status.showMessage(
                "👥 Multi mode — Lead fans out independent features (sequenced in waves by "
                "per-role cost, no fixed cap)",
                6000,
            )
        else:
            self._status.showMessage("👤 1:1 mode — one agent per role", 4000)

    def _on_exec_mode_changed(self, mode: str) -> None:
        """Repaint the execution-mode chip when it flips. Triggered by
        Orchestrator.execModeChanged."""
        if not hasattr(self, "_chip_exec_mode"):
            return
        is_parallel = mode == "parallel"
        self._chip_exec_mode.setText("👥 Multi" if is_parallel else "👤 1:1")
        self._chip_exec_mode.setStyleSheet(self._exec_mode_chip_style(is_parallel))
        self._chip_exec_mode.setToolTip(self._exec_mode_chip_tooltip(is_parallel))

    def _on_auto_resume_chip_clicked(self) -> None:
        """Flip auto-resume ON ↔ OFF on the orchestrator. It persists state,
        broadcasts to live Lead panes, and emits autoResumeChanged → we
        repaint the chip via _on_auto_resume_changed."""
        from . import auto_resume

        target = not auto_resume.is_enabled()
        ok, msg = self.orch.set_auto_resume(target)
        if not ok:
            self._status.showMessage(f"Auto-resume toggle failed: {msg}", 4000)
        elif target:
            self._status.showMessage(
                "🌙 Auto-resume ON — usage-limit panes with a pending task park "
                "and wake automatically at reset",
                6000,
            )
        else:
            self._status.showMessage("🌙 Auto-resume OFF — back to notify-only", 4000)

    def _on_auto_resume_changed(self, enabled: bool) -> None:
        """Repaint the auto-resume chip when it flips. Triggered by
        Orchestrator.autoResumeChanged."""
        if not hasattr(self, "_chip_auto_resume"):
            return
        self._chip_auto_resume.setText("🌙 Auto-resume" if enabled else "🌙 Auto-resume: off")
        self._chip_auto_resume.setStyleSheet(self._auto_resume_chip_style(enabled))
        self._chip_auto_resume.setToolTip(self._auto_resume_chip_tooltip(enabled))

    # ──────────────────────────────────────────────────────────────
    # 🌐 Remote chip: settings dialog + live enable/disable
    # ──────────────────────────────────────────────────────────────

    def _on_remote_chip_clicked(self) -> None:
        """🌐 Remote chip: open the phone-pairing settings dialog.

        Dynamic import only (import-linter's remote-bolt-on-isolation
        contract, pyproject.toml) — a deleted `remote/` package degrades
        this to a status-bar message instead of an ImportError.
        """
        import importlib

        try:
            _dialog_mod = importlib.import_module("agent_takkub.remote.settings_dialog")
            _config_mod = importlib.import_module("agent_takkub.remote.config")
        except ModuleNotFoundError:
            self._status.showMessage("Remote control unavailable — remote/ package not found", 4000)
            self._refresh_remote_chip()
            return

        dlg = _dialog_mod.RemoteSettingsDialog(
            self,
            is_live=getattr(self, "_remote", None) is not None,
            current=_config_mod.RemoteConfig.load(),
            on_apply=self._apply_remote_config,
        )
        dlg.exec()
        self._refresh_remote_chip()

    def _apply_remote_config(self, config, enable: bool) -> tuple[bool, str, str]:
        """Enable/disable the live remote-control server. Injected into
        `RemoteSettingsDialog` as `on_apply` so `remote/` never needs to
        import (or even know the shape of) MainWindow. Dynamic import only,
        same contract as `_on_remote_chip_clicked`.

        Re-enabling while already live (or flipping settings) always stops
        the old handle first, then starts fresh from whatever was just
        saved to disk — `RemoteControl.maybe_start` reloads config from
        disk rather than trusting the `config` argument directly.
        """
        import importlib

        try:
            _remote_mod = importlib.import_module("agent_takkub.remote")
        except ModuleNotFoundError:
            return False, "remote/ package not found", ""

        old = getattr(self, "_remote", None)
        if old is not None:
            old.stop()
            self._remote = None

        if not enable:
            try:
                _config_mod = importlib.import_module("agent_takkub.remote.config")
                cfg = _config_mod.RemoteConfig.load()
                cfg.enabled = False
                cfg.save()
            except ModuleNotFoundError:
                pass
            return True, "", ""

        config.enabled = True
        config.save()
        self._remote = _remote_mod.RemoteControl.maybe_start(self.orch)
        if self._remote is None:
            return False, "Failed to start the remote server — check logs.", ""

        # Quick-tunnel (cloudflared) and ngrok-random mode: the public URL
        # isn't known until the provider prints it, a second or two after
        # start(). ngrok-fixed already knows its URL upfront (set at
        # build_config time) so it's excluded here — `captured_url` is
        # already non-None for it and this loop would just no-op anyway.
        # ponytail: block this click briefly rather than wire a QTimer poll
        # into a dialog that's otherwise fully Qt-event-loop-decoupled.
        # Ceiling: if a real quick tunnel is ever slower than this, the
        # dialog just shows no pairing URL until reopened — upgrade path is
        # a background poll instead of this bounded wait.
        tunnel_obj = getattr(self._remote, "_tunnel", None)
        tunnel_cfg = self._remote.config.tunnel
        needs_url_scrape = tunnel_cfg.type == "quick" or (
            tunnel_cfg.type == "ngrok" and tunnel_cfg.url_mode == "random"
        )
        if tunnel_obj is not None and needs_url_scrape:
            import time as _time

            deadline = _time.monotonic() + _QUICK_TUNNEL_WAIT_S
            while tunnel_obj.captured_url is None and _time.monotonic() < deadline:
                _time.sleep(_QUICK_TUNNEL_POLL_S)
            if tunnel_obj.captured_url:
                self._remote.config.public_url = tunnel_obj.captured_url
                self._remote.config.save()

        return True, "", self._remote.config.pairing_url()

    # ──────────────────────────────────────────────────────────────
    # per-project user profile selector (accessed via 👥 Team chip's right-click menu)
    # ──────────────────────────────────────────────────────────────

    def _on_user_changed(self, name: str) -> None:
        if not name:
            return
        project = active_project()[0] or ""
        if not project:
            return
        from . import user_profile

        old_name = user_profile.profile_for(project)
        old_cd = user_profile.config_dir_for(project)
        try:
            user_profile.set_profile(project, name)
        except ValueError:
            return
        new_cd = user_profile.config_dir_for(project)
        changed = old_cd.resolve() != new_cd.resolve()

        # When the account actually changes, the running Lead + teammate panes
        # still carry the old CLAUDE_CONFIG_DIR (injected once at spawn time),
        # so the switch is invisible until they respawn. Confirm, then kill +
        # respawn so the new profile's login takes effect immediately. Same
        # restart path the active-project switch uses.
        if changed:
            from PyQt6.QtWidgets import QMessageBox

            confirm = QMessageBox.question(
                self,
                "Switch Claude account",
                f"Switch '{project}' to profile '{name}'?\n\n"
                f"Lead + every teammate pane for this project will be "
                f"restarted so the new login takes effect.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok,
            )
            if confirm != QMessageBox.StandardButton.Ok:
                # Backed out — restore the previous selection so the persisted
                # state still matches the (unchanged) running panes.
                try:
                    user_profile.set_profile(project, old_name)
                except ValueError:
                    pass
                return

        if self._limit_store is not None:
            if changed:
                self._limit_store.unregister(old_cd)
                self._limit_store.register(new_cd)
            self._refresh_limit_label(self._limit_store.get(new_cd))

        if changed:
            self._status.showMessage(f"switching {project} → {name} · restarting panes…", 6_000)
            self._restart_lead_for_active_project()

    def _on_add_user_clicked(self) -> None:
        """ "Add / Remove user…" right-click menu entry: jump straight to the
        Settings window's Users tab (2026-07-11) — same not-a-popup pattern
        as the "+ New Role" button's ``_goto_view(VIEW_NEW_ROLE)``, just
        entered from outside an already-open window instead of from within
        one. Supersedes the old standalone ``open_user_profiles_dialog``
        modal QDialog (removed the same day — its Profiles/Claude Auth
        content now lives in ``settings_window.SettingsWindow``'s Users
        view)."""
        from .settings_window import VIEW_USERS

        self._open_settings_window(VIEW_USERS)
