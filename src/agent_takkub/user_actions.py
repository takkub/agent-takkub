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
    # pipeline settings dialog + menu
    # ──────────────────────────────────────────────────────────────

    def _open_pipeline_settings_dialog(self) -> None:
        """Open the pipeline-settings dialog (drag-drop hops, templates,
        provider/role enable) for the **active project**. On Save & Apply the
        page persists templates + per-role enable + per-role CLI to that
        project's files under `~/.takkub/projects/<project>/` via the bridge
        (so tabs don't collide), and stashes the desired provider on/off. We
        then route any *changed* provider through `orchestrator.toggle_provider`
        — which stays GLOBAL (`disabled-providers.json`) — so it repaints the
        status-bar chip AND broadcasts the `[system]` notice to live Lead panes,
        identical to a chip click. Cancel / window-close discards (Rejected).
        """
        from .pipeline_dialog import PipelineSettingsDialog
        from .provider_state import is_disabled

        try:
            from .config import active_project as _active_project

            _proj, _ = _active_project()
        except Exception:
            _proj = None

        dlg = PipelineSettingsDialog(self, project=_proj)
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

    def _show_pipelines_menu(self) -> None:
        """Show the ⚙ Pipelines drop-down menu.

        Built fresh on every click so user-profile state is always current.
        Menu sections:
          1. Pipeline Settings…
          ─────────────────
          2. User profiles (checkable, one per profile)
          ─────────────────
          3. Add / Remove user… (now includes Claude Auth tab)
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

        act_pipeline = QAction("Pipeline Settings…", self)
        act_pipeline.triggered.connect(self._open_pipeline_settings_dialog)
        menu.addAction(act_pipeline)

        menu.addSeparator()

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

    def _on_resume_clicked(self) -> None:
        """Send /resume to the active tab's Lead pane so claude opens its
        session picker. No-op if Lead isn't ready yet — slash injection
        helper drops silently in that case (max wait 45s)."""
        active_project_name = None
        try:
            from .config import active_project as _active_project

            name, _ = _active_project()
            active_project_name = name
        except Exception:
            pass
        self.orch.inject_slash_command_when_ready("lead", "/resume", project=active_project_name)

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

    # ──────────────────────────────────────────────────────────────
    # broadcast actions (UI review, shell, bug check, doctor)
    # ──────────────────────────────────────────────────────────────

    def _on_pane_tools_clicked(self) -> None:
        """🔧 Tools button: open the role x MCP/plugin policy matrix editor."""
        from .pane_tools_dialog import PaneToolsDialog

        dlg = PaneToolsDialog(self)
        dlg.exec()

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
        report_view.setStyleSheet(
            "QPlainTextEdit { background:#18181b; color:#d4d4d8; "
            "border:1px solid #3f3f46; border-radius:4px; padding:6px; "
            "font-family: 'Consolas', 'Courier New', monospace; font-size:12px; }"
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
        btn_fix.setStyleSheet(
            "QPushButton { background:#16a34a; color:#fff; border:none; "
            "border-radius:5px; padding:4px 14px; font-weight:600; }"
            "QPushButton:hover { background:#15803d; }"
            "QPushButton:disabled { background:#3f3f46; color:#71717a; }"
        )

        # 🧩 Install plugins — the old standalone Plugins button folded into
        # Doctor. Installs any missing recommended dev-team plugins on the same
        # background-thread pattern as everything else here (a `claude plugin
        # install` git-clone must never touch the Qt event loop), then re-runs
        # the checks so the [plugins] findings refresh in place.
        btn_plugins = QPushButton("Install plugins", dlg)
        btn_plugins.setStyleSheet(
            "QPushButton { background:#2563eb; color:#fff; border:none; "
            "border-radius:5px; padding:4px 14px; font-weight:600; }"
            "QPushButton:hover { background:#1d4ed8; }"
            "QPushButton:disabled { background:#3f3f46; color:#71717a; }"
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
            "QPushButton { background:transparent; color:#a1a1aa; "
            "border:1px solid #3f3f46; border-radius:5px; padding:4px 14px; }"
            "QPushButton:hover { background:#27272a; color:#d4d4d8; }"
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
            cap = exec_mode.machine_fanout_cap()
            self._status.showMessage(
                f"👥 Multi mode — Lead fans out independent features (≤{cap}/role on this machine)",
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
    # per-project user profile selector (accessed via ⚙ Pipelines menu)
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
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QFrame,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QMessageBox,
            QPushButton,
            QTabWidget,
            QVBoxLayout,
            QWidget,
        )

        from . import user_profile
        from .claude_auth_config import ClaudeAuthConfig, load_claude_auth, save_claude_auth

        dlg = QDialog(self)
        dlg.setWindowTitle("User Profiles & Claude Auth")
        dlg.resize(560, 440)
        main_lay = QVBoxLayout(dlg)

        tabs = QTabWidget(dlg)
        main_lay.addWidget(tabs)

        # ──────────────────────────────────────────────────────────────
        # Tab 1: Profiles (existing Add/Remove user content)
        # ──────────────────────────────────────────────────────────────
        profile_tab = QWidget()
        lay = QVBoxLayout(profile_tab)

        lay.addWidget(QLabel("Existing profiles ('default' cannot be removed):"))
        profile_list = QListWidget(profile_tab)
        profiles: list[dict] = user_profile.list_profiles()
        for p in profiles:
            profile_list.addItem(f"{p['name']}  →  {p['config_dir']}")
        lay.addWidget(profile_list)

        btn_row_w = QWidget(profile_tab)
        btn_row_l = QHBoxLayout(btn_row_w)
        btn_row_l.setContentsMargins(0, 0, 0, 0)
        btn_remove = QPushButton("Remove selected", profile_tab)
        btn_remove.setEnabled(False)
        btn_share = QPushButton("🔗 Share sessions with default", profile_tab)
        btn_share.setEnabled(False)
        btn_share.setToolTip(
            "Convert this profile to shared-session mode: its existing\n"
            "sessions/todos/plugins/skills are merged into the default\n"
            "profile (nothing overwritten, originals kept as *.pre-share-backup),\n"
            "then linked — from then on switching users changes ONLY the\n"
            "account; history and plugins are the same everywhere."
        )
        btn_row_l.addWidget(btn_remove)
        btn_row_l.addWidget(btn_share)
        lay.addWidget(btn_row_w)

        def _on_sel(row: int) -> None:
            btn_remove.setEnabled(row > 0)  # row 0 = "default", not removable
            btn_share.setEnabled(row > 0)

        profile_list.currentRowChanged.connect(_on_sel)

        def _do_remove() -> None:
            row = profile_list.currentRow()
            if row <= 0 or row >= len(profiles):
                return
            try:
                user_profile.remove_profile(profiles[row]["name"])
            except ValueError as exc:
                QMessageBox.warning(dlg, "Cannot remove", str(exc))
                return
            # Unlink shared junctions FIRST so a later manual delete of the
            # profile folder can't traverse a junction into ~/.claude data.
            try:
                user_profile.cleanup_profile_links(profiles[row]["config_dir"])
            except Exception:
                pass
            profile_list.takeItem(row)
            profiles.pop(row)

        btn_remove.clicked.connect(_do_remove)

        def _do_share() -> None:
            row = profile_list.currentRow()
            if row <= 0 or row >= len(profiles):
                return
            p = profiles[row]
            confirm = QMessageBox.question(
                dlg,
                "Share sessions?",
                f"Convert '{p['name']}' ({p['config_dir']}) to shared-session mode?\n\n"
                "• Its sessions/todos/plugins/skills merge into the default\n"
                "  profile — nothing is overwritten, originals are kept as\n"
                "  *.pre-share-backup inside the profile dir.\n"
                "• Login/credentials stay separate — only the account differs.\n"
                "• Panes already open keep their old view until respawned.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok,
            )
            if confirm != QMessageBox.StandardButton.Ok:
                return
            results = user_profile.convert_profile_to_shared(p["config_dir"])
            QMessageBox.information(
                dlg,
                "Shared-session conversion",
                "\n".join(f"{k}: {v}" for k, v in results.items()),
            )

        btn_share.clicked.connect(_do_share)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #3f3f46;")
        lay.addWidget(sep)
        lay.addWidget(QLabel("Add new profile:"))
        form = QFormLayout()
        name_edit = QLineEdit(profile_tab)
        name_edit.setPlaceholderText("e.g. work, personal")
        dir_edit = QLineEdit(profile_tab)
        dir_edit.setPlaceholderText("path to Claude config dir, e.g. ~/.claude-work")
        dir_row_w = QWidget(profile_tab)
        dir_row_l = QHBoxLayout(dir_row_w)
        dir_row_l.setContentsMargins(0, 0, 0, 0)
        dir_row_l.addWidget(dir_edit)
        btn_browse = QPushButton("Browse…", profile_tab)
        btn_browse.setFixedWidth(72)
        dir_row_l.addWidget(btn_browse)
        form.addRow("Name:", name_edit)
        form.addRow("Config dir:", dir_row_w)
        from PyQt6.QtWidgets import QCheckBox

        share_chk = QCheckBox("🔗 Share sessions/plugins with default (switch account only)")
        share_chk.setChecked(True)
        share_chk.setToolTip(
            "Recommended. The new profile links sessions/todos/plugins/skills\n"
            "to the default profile — switching users changes ONLY the login.\n"
            "Uncheck for a fully isolated profile (old behaviour).\n"
            "Leave Config dir blank to use ~/.claude-<name>."
        )
        form.addRow("", share_chk)
        lay.addLayout(form)

        def _do_browse() -> None:
            d = QFileDialog.getExistingDirectory(dlg, "Select Claude config directory")
            if d:
                dir_edit.setText(d)

        btn_browse.clicked.connect(_do_browse)

        btn_add = QPushButton("Add Profile", profile_tab)
        lay.addWidget(btn_add)

        def _do_add() -> None:
            n = name_edit.text().strip()
            d = dir_edit.text().strip()
            if not n:
                return
            if not d:
                if not share_chk.isChecked():
                    return  # isolated profiles must name their dir explicitly
                from pathlib import Path as _P

                d = str(_P.home() / f".claude-{n}")
            try:
                linked = user_profile.add_profile(n, d, share_sessions=share_chk.isChecked())
            except ValueError as exc:
                QMessageBox.warning(dlg, "Invalid profile", str(exc))
                return
            new_p = {"name": n, "config_dir": d}
            profiles.append(new_p)
            suffix = "  🔗shared" if linked else ""
            profile_list.addItem(f"{n}  →  {d}{suffix}")
            name_edit.clear()
            dir_edit.clear()
            if linked:
                self._status.showMessage(
                    f"👤 profile '{n}' created — shares {', '.join(linked)} with default · "
                    "run 'claude login' in a pane of that profile to sign in",
                    9_000,
                )

        btn_add.clicked.connect(_do_add)

        tabs.addTab(profile_tab, "Profiles")

        # ──────────────────────────────────────────────────────────────
        # Tab 2: Claude Auth (embedded from ClaudeAuthDialog content)
        # ──────────────────────────────────────────────────────────────
        auth_tab = QWidget()
        auth_lay = QVBoxLayout(auth_tab)
        auth_lay.setSpacing(10)

        intro = QLabel(
            "Point a profile's Claude Code panes at a different backend — DeepSeek,\n"
            "OpenRouter, a local model — instead of Anthropic. These settings are\n"
            "saved *per profile*: leave them blank and that profile keeps its normal\n"
            "Claude login; set a base URL and only that profile's panes use the API.\n"
            "Applies to the next pane you spawn (restart open panes to pick it up)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #d4d4d8;")
        auth_lay.addWidget(intro)

        # Which profile are we editing? Each profile stores its own auth file in
        # its config_dir, so switching here loads/saves that profile in isolation.
        auth_profiles = user_profile.list_profiles()  # [{name, config_dir}, ...]

        def _auth_dir(profile_name: str):
            """config_dir for *profile_name* (None → default ~/.claude)."""
            for p in auth_profiles:
                if p["name"] == profile_name:
                    return Path(p["config_dir"])
            return None

        sel_row = QHBoxLayout()
        sel_row.setContentsMargins(0, 0, 0, 0)
        sel_row.addWidget(QLabel("Settings for profile:"))
        auth_profile_combo = QComboBox(auth_tab)
        for p in auth_profiles:
            auth_profile_combo.addItem(p["name"])
        auth_profile_combo.setToolTip(
            "Each profile has its own auth. Switching reloads that profile's saved\n"
            "values from disk — Save before switching to keep unsaved edits."
        )
        sel_row.addWidget(auth_profile_combo, 1)
        auth_lay.addLayout(sel_row)

        auth_form = QFormLayout()
        auth_form.setHorizontalSpacing(16)
        auth_form.setVerticalSpacing(8)
        auth_lay.addLayout(auth_form)

        base_url_edit = QLineEdit()
        base_url_edit.setPlaceholderText(
            "blank = Anthropic  ·  e.g. https://api.deepseek.com/anthropic"
        )
        auth_form.addRow("Base URL:", base_url_edit)

        api_key_edit = QLineEdit()
        api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_edit.setPlaceholderText("your provider's API key  ·  blank = none")
        auth_form.addRow("API key:", api_key_edit)

        auth_token_edit = QLineEdit()
        auth_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        auth_token_edit.setPlaceholderText(
            "usually blank — the API key above is reused as the bearer token"
        )
        auth_form.addRow("Auth token:", auth_token_edit)

        note = QLabel(
            "Examples:\n"
            "• DeepSeek — Base URL: https://api.deepseek.com/anthropic + API key: your DeepSeek key\n"
            "• OpenRouter — Base URL: https://openrouter.ai/api + Auth token: your OpenRouter key\n"
            "  (then add ANTHROPIC_DEFAULT_SONNET_MODEL below to choose the model)"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #a1a1aa;")
        auth_lay.addWidget(note)

        env_label = QLabel(
            "Extra environment variables — sent to every pane. Use for a provider key,\n"
            "or to pick a model (e.g. ANTHROPIC_DEFAULT_SONNET_MODEL = qwen/qwen3-coder:free):"
        )
        env_label.setWordWrap(True)
        env_label.setStyleSheet("color: #d4d4d8; padding-top: 4px;")
        auth_lay.addWidget(env_label)

        env_rows: list[tuple[QLineEdit, QLineEdit, QWidget]] = []
        rows_box = QVBoxLayout()
        rows_box.setSpacing(4)
        auth_lay.addLayout(rows_box)

        def _add_env_row(name: str = "", value: str = "") -> None:
            row = QWidget(auth_tab)
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)

            name_edit = QLineEdit(name)
            name_edit.setPlaceholderText("NAME — e.g. ANTHROPIC_DEFAULT_SONNET_MODEL")
            value_edit = QLineEdit(value)
            value_edit.setPlaceholderText("value — e.g. qwen/qwen3-coder:free")
            remove_btn = QPushButton("✕", row)
            remove_btn.setFixedWidth(28)
            remove_btn.setToolTip("Remove this variable")

            h.addWidget(name_edit, 2)
            h.addWidget(value_edit, 3)
            h.addWidget(remove_btn, 0)

            entry = (name_edit, value_edit, row)
            env_rows.append(entry)
            rows_box.addWidget(row)

            def _remove() -> None:
                if entry in env_rows:
                    env_rows.remove(entry)
                rows_box.removeWidget(row)
                row.deleteLater()

            remove_btn.clicked.connect(_remove)

        add_env_btn = QPushButton("+ Add variable", auth_tab)
        add_env_btn.clicked.connect(lambda: _add_env_row())
        auth_lay.addWidget(add_env_btn)

        def _clear_env_rows() -> None:
            for _n, _v, row in list(env_rows):
                rows_box.removeWidget(row)
                row.deleteLater()
            env_rows.clear()

        def _load_auth_profile(profile_name: str) -> None:
            """Populate the auth fields from *profile_name*'s saved config."""
            loaded = load_claude_auth(_auth_dir(profile_name))
            base_url_edit.setText(loaded.base_url)
            api_key_edit.setText(loaded.api_key)
            auth_token_edit.setText(loaded.auth_token)
            _clear_env_rows()
            for name, value in loaded.extra_env.items():
                _add_env_row(name, value)
            if not env_rows:
                _add_env_row()

        auth_profile_combo.currentTextChanged.connect(_load_auth_profile)
        _load_auth_profile(auth_profile_combo.currentText())  # seed initial view

        tabs.addTab(auth_tab, "Claude Auth")

        # ──────────────────────────────────────────────────────────────
        # Bottom buttons
        # ──────────────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close, dlg
        )

        def _on_save() -> None:
            # Save the Claude Auth tab to the *currently selected* profile only.
            profile_name = auth_profile_combo.currentText()
            env_dict: dict[str, str] = {}
            for name_ed, value_ed, _row in env_rows:
                name = name_ed.text().strip()
                if name:
                    env_dict[name] = value_ed.text()

            try:
                save_claude_auth(
                    ClaudeAuthConfig(
                        base_url=base_url_edit.text(),
                        api_key=api_key_edit.text(),
                        auth_token=auth_token_edit.text(),
                        extra_env=env_dict,
                    ),
                    _auth_dir(profile_name),
                )
                self._status.showMessage(
                    f"Claude auth saved for profile '{profile_name}' — respawn its "
                    "panes to use the new settings.",
                    7_000,
                )
            except OSError as e:
                QMessageBox.critical(
                    dlg, "Save failed", f"Couldn't write takkub-claude-auth.json:\n{e}"
                )
                return

        btns.button(QDialogButtonBox.StandardButton.Save).clicked.connect(_on_save)
        btns.button(QDialogButtonBox.StandardButton.Close).clicked.connect(dlg.accept)
        main_lay.addWidget(btns)

        dlg.exec()
