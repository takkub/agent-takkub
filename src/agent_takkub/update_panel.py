"""MainWindowUpdateMixin — cockpit/Claude-CLI self-update UX (refactor round 3, step A).

Extracted from ``MainWindow`` as a mixin. All methods access ``self.*``
attributes (``_btn_update``, ``_update_status_cache``, ``_update_worker_busy``,
``orch``, etc.) initialised in ``MainWindow.__init__``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QCoreApplication, QThread, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QSystemTrayIcon

from . import cockpit_theme, config
from .config import REPO_ROOT, active_project, lead_cwd
from .orchestrator import _log_event
from .rtk_helper import install_rtk, rtk_hook_enabled, set_rtk_enabled


def _release_port_file() -> None:
    """Delete the *effective* port file (``config._get_port_file()``) so a
    successor cockpit / fresh boot never reconnects to a stale port number.

    Uses the effective path (honours a multi-instance/custom
    ``TAKKUB_PORT_FILE`` override) rather than the static ``config.PORT_FILE``
    constant — see docs/audit/2026-07-05-isolation-plan-crosscheck-codex.md,
    finding 5."""
    try:
        pf = config._get_port_file()
        if pf.exists():
            pf.unlink()
    except Exception:
        pass


class _NpmUpdateThread(QThread):
    """npm registry check / global update OFF the Qt main thread.

    mode="check"   → emits done(ok, current, latest, msg)
    mode="install" → runs `npm install -g agent-takkub@latest` (postinstall
                     upgrades the ~/.agent-takkub venv wheel) then emits done.
    Held in the module-level _NPM_THREADS set — same lifetime rules as the
    doctor/plugin workers in user_actions (never parented to the window).
    """

    done: pyqtSignal = pyqtSignal(bool, str, str, str)  # ok, current, latest, msg

    def __init__(self, mode: str, parent=None) -> None:
        super().__init__(parent)
        self._mode = mode

    def run(self) -> None:  # pragma: no cover - thin subprocess wrapper
        import shutil as _shutil
        import subprocess as _subprocess
        from importlib import metadata as _metadata

        from ._win_console import SUBPROCESS_NO_WINDOW

        try:
            current = _metadata.version("agent-takkub")
        except Exception:
            current = "?"
        npm = _shutil.which("npm.cmd") or _shutil.which("npm")
        if not npm:
            self.done.emit(False, current, "", "npm not found on PATH")
            return
        try:
            if self._mode == "check":
                r = _subprocess.run(
                    [npm, "view", "agent-takkub", "version"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    creationflags=SUBPROCESS_NO_WINDOW,
                )
                latest = (r.stdout or "").strip()
                if r.returncode != 0 or not latest:
                    self.done.emit(
                        False, current, "", (r.stderr or "registry check failed").strip()[-200:]
                    )
                    return
                self.done.emit(True, current, latest, "")
            else:
                r = _subprocess.run(
                    [npm, "install", "-g", "agent-takkub@latest"],
                    capture_output=True,
                    text=True,
                    timeout=900,
                    creationflags=SUBPROCESS_NO_WINDOW,
                )
                if r.returncode != 0:
                    tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
                    self.done.emit(False, current, "", tail[-1] if tail else "npm install failed")
                    return
                self.done.emit(True, current, "", "updated")
        except Exception as e:
            self.done.emit(False, current, "", str(e))


_NPM_THREADS: set = set()


class MainWindowUpdateMixin:
    """Mixin for cockpit self-update and Claude-CLI update UX."""

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

        # npm/pip installs have no git checkout, so the git poll returns
        # `not_repo` and can't tell whether a newer build was published. Kick
        # off a background npm-registry check so the chip can flip colour just
        # like the git behind-state does.
        if status.get("not_repo"):
            from .config import is_installed_package

            if is_installed_package():
                self._schedule_npm_update_check()

        self._refresh_update_button()

    def _schedule_npm_update_check(self) -> None:
        """Background (modal-free) npm-registry version check for installed
        cockpits. Runs on the same 5-min cadence as the git poll via
        `_on_update_check_done`; a quiet sibling of `_start_npm_update_check`
        (the click path, which shows dialogs). Skips if one is already in
        flight so polls never stack."""
        if self._npm_check_busy:
            return
        self._npm_check_busy = True
        worker = _NpmUpdateThread("check")
        _NPM_THREADS.add(worker)
        worker.done.connect(self._on_npm_update_check_done)
        worker.finished.connect(lambda w=worker: _NPM_THREADS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_npm_update_check_done(self, ok: bool, current: str, latest: str, msg: str) -> None:
        """Cache the npm-registry result and recolour the chip. Best-effort:
        a failed check keeps the last good cache (chip stays neutral green)
        rather than false-alarming."""
        self._npm_check_busy = False
        if ok and latest:
            self._npm_update_cache = {"ok": True, "current": current, "latest": latest}
        elif self._npm_update_cache is None:
            self._npm_update_cache = {"ok": False, "current": current, "latest": ""}
        try:
            self._refresh_update_button()
        except RuntimeError:
            pass  # window closed while the check ran

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
        try:
            if _count >= flashes * 2:
                # Restore to whatever _refresh_update_button would set.
                self._refresh_update_button()
                return
            if _count % 2 == 0:
                self._btn_update.setStyleSheet(
                    f"QPushButton {{ color: {cockpit_theme.BANNER_WARN_TEXT}; "
                    f"background: {cockpit_theme.BANNER_WARN_BG}; "
                    f"border: 2px solid {cockpit_theme.STATE_EXITED}; border-radius: 4px; "
                    "padding: 2px 8px; font-weight: 600; }"
                )
            else:
                self._btn_update.setStyleSheet(
                    f"QPushButton {{ color: {cockpit_theme.BANNER_WARN_TEXT}; "
                    f"background: {cockpit_theme.BANNER_WARN_BG}; "
                    f"border: 1px solid {cockpit_theme.BANNER_WARN_BORDER}; border-radius: 4px; "
                    "padding: 2px 8px; }"
                )
            QTimer.singleShot(
                400,
                lambda count=_count + 1: self._pulse_update_button(flashes, count),
            )
        except RuntimeError:
            return

    # ------------------------------------------------------------------
    # Legacy entry point kept for callers that request an immediate check.
    # It now delegates to the same background worker as recurring polls.
    # ------------------------------------------------------------------

    def _run_update_check(self) -> None:
        """Schedule the early-boot fallback check without blocking Qt."""
        self._schedule_update_check()

    # ------------------------------------------------------------------
    # Claude CLI update (separate from cockpit self-update above)
    # ------------------------------------------------------------------

    # NOTE: _on_claude_update_clicked was removed with the ⬆ Claude CLI button.
    # The native worker/dialog self-update machinery below
    # (_on_claude_update_check_done, _show_claude_update_dialog,
    # _confirm_and_apply_claude_update) is kept for the detached close-panes
    # update path but is not wired to any widget.

    def _on_claude_update_check_done(self, result: dict) -> None:
        """Render the worker result: fatal error → warning; no update → toast;
        update available → report dialog."""
        from PyQt6.QtWidgets import QMessageBox

        self._claude_update_busy = False
        # The ⬆ Claude CLI button was removed; guard the native-path restore so
        # this slot can't AttributeError if the detached worker ever fires.
        _cu_btn = getattr(self, "_btn_claude_update", None)
        if _cu_btn is not None:
            _cu_btn.setEnabled(True)
            _cu_btn.setText("⬆ Claude CLI")
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
        header.setStyleSheet(
            f"color:{cockpit_theme.TEXT_PRIMARY_ALT}; font-size:13px; padding:2px;"
        )
        layout.addWidget(header)

        # Issue auto-filing status (the user wants action-needed findings filed
        # to GitHub so they can fix later). Show what happened.
        issue_line = ""
        if result.get("issue_error"):
            issue_line = f"⚠️ เปิด issue ไม่สำเร็จ: {result['issue_error']}"
            issue_color = cockpit_theme.BANNER_ERROR_TEXT
        elif result.get("issue_skipped") and result.get("issue_number"):
            issue_line = (
                f"📋 มี issue เดิมสำหรับ version นี้อยู่แล้ว: #{result['issue_number']} "
                f"({result.get('issue_url', '')})"
            )
            issue_color = cockpit_theme.ROLE_COLOR_FALLBACK
        elif result.get("issue_number"):
            issue_line = (
                f"📋 เปิด GitHub issue ให้แล้ว: #{result['issue_number']} "
                f"({result.get('issue_url', '')}) — มาสั่งแก้ทีหลังได้"
            )
            issue_color = cockpit_theme.BANNER_OK_TEXT
        elif result.get("analysis_ok") and not result.get("issue_action_required"):
            issue_line = "✅ AI ประเมินว่าไม่ต้องแก้ระบบ — ไม่เปิด issue"
            issue_color = cockpit_theme.ROLE_COLOR_FALLBACK
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
            f"QTextBrowser {{ background:{cockpit_theme.GROUND_SIDEBAR}; "
            f"color:{cockpit_theme.TEXT_PRIMARY_ALT}; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; border-radius:6px; padding:8px; }}"
        )
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        update_btn = QPushButton(f"⬆ อัพเดตเป็น v{latest}", dlg)
        # Primary CTA → gold (was a blue #2563eb fill).
        update_btn.setStyleSheet(
            f"QPushButton {{ color:{cockpit_theme.GOLD_TEXT_ON}; "
            f"background:{cockpit_theme.GOLD_GRAD_BOTTOM}; border:none; "
            "border-radius:4px; padding:4px 12px; font-weight:700; }"
            f"QPushButton:hover {{ background:{cockpit_theme.GOLD_GRAD_TOP}; }}"
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
        from .orchestrator import _split_shard as _mw_split_shard2
        from .provider_config import effective_provider_for

        n = 0
        for project_panes in self.orch._panes_by_project.values():
            for role, pane in project_panes.items():
                sess = getattr(pane, "session", None)
                if sess is not None and getattr(sess, "is_alive", False):
                    if effective_provider_for(_mw_split_shard2(role)[0]) == "claude":
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
        _release_port_file()

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
            cockpit_pid=os.getpid(),
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

    # ── npm-install update flow (installed builds have no git checkout) ──

    def _start_npm_update_check(self) -> None:
        """Check the npm registry for a newer agent-takkub, then offer a
        one-click update + auto-restart. Both subprocesses run off the Qt
        main thread (_NpmUpdateThread)."""
        self._status.showMessage("🔄 Checking npm registry…", 8_000)
        worker = _NpmUpdateThread("check")
        _NPM_THREADS.add(worker)

        def _checked(ok: bool, current: str, latest: str, msg: str) -> None:
            try:
                if not ok:
                    QMessageBox.warning(self, "Update check failed", msg)
                    return
                if latest == current:
                    self._status.showMessage(f"🔄 Up to date (v{current})", 6_000)
                    return
                confirm = QMessageBox.question(
                    self,
                    "Update available",
                    f"agent-takkub v{latest} is available (you have v{current}).\n\n"
                    "Update now? The cockpit will restart itself when done —\n"
                    "pane state is saved and teammates respawn automatically.\n"
                    "Your projects + config in ~/.agent-takkub stay untouched.",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Ok,
                )
                if confirm == QMessageBox.StandardButton.Ok:
                    self._start_npm_update_install(latest)
            except RuntimeError:
                pass  # window closed while the worker ran

        worker.done.connect(_checked)
        worker.finished.connect(lambda w=worker: _NPM_THREADS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _start_npm_update_install(self, latest: str) -> None:
        self._btn_update.setEnabled(False)
        self._btn_update.setText(f"⬇ Updating to v{latest}…")
        self._status.showMessage(
            f"⬇ npm install -g agent-takkub@{latest} — venv upgrade may take a few minutes…", 0
        )
        worker = _NpmUpdateThread("install")
        _NPM_THREADS.add(worker)

        def _installed(ok: bool, current: str, _latest: str, msg: str) -> None:
            try:
                self._btn_update.setEnabled(True)
                self._status.clearMessage()
                if not ok:
                    self._btn_update.setText("🔄 Update via npm")
                    QMessageBox.critical(
                        self,
                        "Update failed",
                        f"{msg}\n\nNothing was restarted — the cockpit keeps running\n"
                        "on the current version. You can retry, or run\n"
                        "`npm install -g agent-takkub@latest` in a terminal.",
                    )
                    return
                _log_event("cockpit_npm_update", version=latest)
                # Same no-dialog restart path as `takkub restart`: state is
                # persisted, the successor waits on the single-instance lock.
                self._restart_cockpit()
            except RuntimeError:
                pass

        worker.done.connect(_installed)
        worker.finished.connect(lambda w=worker: _NPM_THREADS.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _refresh_update_button(self) -> None:
        """Flip the update chip's label/colour based on the cached
        status. Five visual states: not-a-repo, error, clean+up-to-date,
        clean+behind, dirty."""
        status = self._update_status_cache or {}
        if status.get("not_repo"):
            from .config import is_installed_package

            if is_installed_package():
                # npm/pip install → updates come from the package manager, NOT
                # by converting site-packages into a checkout of the (private)
                # upstream repo. Colour by the cached npm-registry check so the
                # chip stands out (blue) when a newer build is published and
                # stays neutral (green) when up-to-date — parity with the
                # git-checkout behind-state.
                from .claude_update import compare_versions

                npm = self._npm_update_cache or {}
                cur = npm.get("current") or ""
                latest = npm.get("latest") or ""
                if npm.get("ok") and cur and latest and compare_versions(cur, latest) < 0:
                    self._btn_update.setText(f"📦 Update available (v{latest})")
                    self._btn_update.setToolTip(
                        f"agent-takkub v{latest} is on npm (you have v{cur}).\n"
                        "Click to update in one go — the cockpit restarts itself\n"
                        "afterwards. Your projects + config in ~/.agent-takkub stay untouched."
                    )
                    self._btn_update.setStyleSheet(
                        f"QPushButton {{ color: {cockpit_theme.BANNER_INFO_BG}; "
                        f"background: {cockpit_theme.BANNER_INFO_TEXT}; "
                        f"border: 1px solid {cockpit_theme.BANNER_INFO_BORDER}; "
                        "border-radius: 4px; padding: 2px 8px; font-weight: 500; }"
                        f"QPushButton:hover {{ background: {cockpit_theme.BANNER_INFO_HOVER}; }}"
                    )
                    return
                self._btn_update.setText("🔄 Update via npm")
                self._btn_update.setToolTip(
                    "Installed via npm. Click to check the registry and update\n"
                    "in one go — the cockpit restarts itself afterwards.\n"
                    "Your projects + config in ~/.agent-takkub stay untouched."
                )
                self._btn_update.setStyleSheet(
                    f"QPushButton {{ color: {cockpit_theme.BANNER_OK_TEXT}; "
                    f"background: {cockpit_theme.BANNER_OK_BG}; "
                    f"border: 1px solid {cockpit_theme.BANNER_OK_BORDER}; "
                    "border-radius: 4px; padding: 2px 8px; }"
                    f"QPushButton:hover {{ background: {cockpit_theme.BANNER_OK_HOVER}; }}"
                )
                return
            self._btn_update.setText("🔧 Enable updates")
            self._btn_update.setToolTip(
                "This install isn't git-tracked. Click to convert it into a\n"
                "git checkout linked to the official repo — enables one-click\n"
                "updates from then on. Your projects.json / runtime/ / .venv/\n"
                "are gitignored and stay safe."
            )
            self._btn_update.setStyleSheet(
                f"QPushButton {{ color: {cockpit_theme.BANNER_WARN_TEXT}; "
                f"background: {cockpit_theme.BANNER_WARN_BG}; "
                f"border: 1px solid {cockpit_theme.BANNER_WARN_BORDER}; "
                "border-radius: 4px; padding: 2px 8px; }"
                f"QPushButton:hover {{ background: {cockpit_theme.BANNER_WARN_HOVER}; }}"
            )
            return
        if not status.get("ok"):
            self._btn_update.setText("⚠ Update check failed")
            self._btn_update.setToolTip(
                f"Last error: {status.get('error', 'unknown')}\nRestart cockpit to retry."
            )
            self._btn_update.setStyleSheet(
                f"QPushButton {{ color: {cockpit_theme.BANNER_ERROR_TEXT}; "
                f"background: {cockpit_theme.BANNER_ERROR_BG}; "
                f"border: 1px solid {cockpit_theme.BANNER_ERROR_BORDER}; "
                "border-radius: 4px; padding: 2px 8px; }"
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
                f"QPushButton {{ color: {cockpit_theme.BANNER_WARN_TEXT}; "
                f"background: {cockpit_theme.BANNER_WARN_BG}; "
                f"border: 1px solid {cockpit_theme.BANNER_WARN_BORDER}; "
                "border-radius: 4px; padding: 2px 8px; }"
            )
            return
        behind = status.get("behind", 0)
        if behind == 0:
            self._btn_update.setText("🔄 Up to date")
            self._btn_update.setToolTip("On origin/main. Next check in 5 min.")
            self._btn_update.setStyleSheet(
                f"QPushButton {{ color: {cockpit_theme.BANNER_OK_TEXT}; "
                f"background: {cockpit_theme.BANNER_OK_BG}; "
                f"border: 1px solid {cockpit_theme.BANNER_OK_BORDER}; "
                "border-radius: 4px; padding: 2px 8px; }"
                f"QPushButton:hover {{ background: {cockpit_theme.BANNER_OK_HOVER}; }}"
            )
        else:
            self._btn_update.setText(f"📦 Update available ({behind})")
            self._btn_update.setToolTip(
                f"origin/main has {behind} new commit{'s' if behind != 1 else ''}. Click to pull."
            )
            self._btn_update.setStyleSheet(
                f"QPushButton {{ color: {cockpit_theme.BANNER_INFO_BG}; "
                f"background: {cockpit_theme.BANNER_INFO_TEXT}; "
                f"border: 1px solid {cockpit_theme.BANNER_INFO_BORDER}; "
                "border-radius: 4px; padding: 2px 8px; font-weight: 500; }"
                f"QPushButton:hover {{ background: {cockpit_theme.BANNER_INFO_HOVER}; }}"
            )

    def _on_update_clicked(self) -> None:
        """User clicked the update chip. Branches by cached status:
        not-a-repo (info), dirty (block + show file list), clean +
        up-to-date (no-op toast), clean + behind (single confirm
        dialog → pull → auto-restart).
        """
        from PyQt6.QtWidgets import QMessageBox

        # First poll fires 30 s after boot. If the user clicks during
        # that window the cache is still None — don't render a fake
        # "check failed" dialog. Kick the check off immediately and
        # tell the user to retry in a moment.
        if self._update_status_cache is None:
            self._status.showMessage("Checking for updates… click again in a moment.", 4_000)
            # Threaded — a synchronous fetch here froze the Qt main thread for up
            # to ~10 s (git fetch network timeout) right after boot. The worker
            # populates the cache via _on_update_check_done; the message already
            # tells the user to retry in a moment.
            self._schedule_update_check()
            return
        status = self._update_status_cache
        if status.get("not_repo"):
            from .config import is_installed_package

            if is_installed_package():
                self._start_npm_update_check()
                return

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

        # Detect a dependency change up front. When pyproject.toml is in the
        # incoming diff the restart must also re-run `pip install -e .` or the
        # new version boots against stale deps — the gap that left other
        # machines "code-updated but not really equal". We now sync deps
        # automatically (detached, post-quit) instead of asking the user to.
        deps_changing = pyproject_will_change_on_pull()
        pip_note = (
            "\n\n📦 New dependencies detected — they'll be installed\n"
            "automatically (pip install -e .) before the cockpit\n"
            "comes back, so this restart takes a little longer."
            if deps_changing
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
            f"are touched.{pip_note}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        from .update_worker import PullUpdateWorker

        self._update_worker_busy = True
        self._btn_update.setEnabled(False)
        self._status.showMessage("Pulling update…", 0)
        worker = PullUpdateWorker()
        worker.signals.finished.connect(
            lambda result, deps=deps_changing: self._on_pull_update_done(result, deps)
        )
        QThreadPool.globalInstance().start(worker)

    def _on_pull_update_done(self, result: dict, deps_changing: bool) -> None:
        """Apply a background pull result on the Qt thread and restart."""
        self._update_worker_busy = False
        try:
            self._btn_update.setEnabled(True)
        except RuntimeError:
            return
        msg = result.get("message", "update failed")
        if not result.get("ok"):
            QMessageBox.critical(self, "Pull failed", msg)
            self._schedule_update_check()
            return
        if deps_changing:
            self._status.showMessage(f"{msg} — syncing dependencies + restarting…", 8_000)
            QTimer.singleShot(500, self._restart_with_pip_sync)
        else:
            self._status.showMessage(f"{msg} — restarting…", 6_000)
            QTimer.singleShot(500, self._restart_cockpit)

    def _restart_with_pip_sync(self) -> None:
        """Restart after a self-update that changed dependencies, re-running
        `pip install -e .` first via a detached script so the new cockpit boots
        with the new deps installed (#self-update completeness).

        Mirrors the Claude-CLI detached-updater pattern: persist state, hand a
        script to a DETACHED process, quit. The script waits for THIS process to
        exit, runs pip in the venv (nothing holding it), then relaunches the
        cockpit — relaunching even if pip fails so the user is never bricked.

        On any failure to set up the detached path we fall back to a plain
        restart: the code is already pulled, so worst case is the previous
        behaviour (boot against stale deps + the user re-runs pip), never worse.
        """
        import subprocess
        import sys

        from .update_helper import build_pip_sync_script

        # Persist up-front — we quit() right after spawning, can't rely on closeEvent.
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
        _release_port_file()

        is_win = sys.platform == "win32"
        runtime_dir = REPO_ROOT / "runtime"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            log_path = runtime_dir / "pip_sync.log"
            script_path = runtime_dir / ("pip_sync.ps1" if is_win else "pip_sync.sh")
            script = build_pip_sync_script(
                python_exe=sys.executable,
                repo_root=str(REPO_ROOT),
                log_path=str(log_path),
                is_windows=is_win,
                cockpit_pid=os.getpid(),
            )
            script_path.write_text(script, encoding="utf-8")
            _log_event("pip_sync_start")
            if is_win:
                import shutil as _shutil

                pwsh = _shutil.which("pwsh") or _shutil.which("powershell") or "powershell"
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so it outlives us.
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
            # Couldn't set up the detached pip-sync — fall back to a plain
            # restart (code is already pulled; this is the old behaviour).
            _log_event("pip_sync_spawn_failed", err=f"{type(e).__name__}: {e}")
            self._restart_cockpit()
            return

        QCoreApplication.quit()

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
        _release_port_file()

        try:
            import os as _os

            # Tag the successor so its single-instance guard WAITS for this
            # process to exit (WebEngine teardown takes seconds) instead of
            # racing into auto-kill or the "already running" OK dialog.
            _succ_env = _os.environ.copy()
            _succ_env["TAKKUB_RESTART_SUCCESSOR"] = "1"
            subprocess.Popen(
                [sys.executable, "-m", "agent_takkub"],
                cwd=str(REPO_ROOT),
                close_fds=True,
                creationflags=SUBPROCESS_NO_WINDOW,
                env=_succ_env,
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
        """Toggle the central rtk hook on/off. It's a personal, central switch
        now (injected at spawn via --settings) — not a per-project install — so
        turning it off simply flips the flag; no repo files are touched either
        way. Legacy per-project cleanup still runs on enable when a project is
        active."""
        from PyQt6.QtWidgets import QMessageBox

        root = lead_cwd()
        if rtk_hook_enabled():
            # Currently ON → turn OFF. No confirm needed (fully reversible, no
            # files written); newly spawned panes just stop getting the hook.
            set_rtk_enabled(False)
            self._status.showMessage(
                "rtk disabled — new panes won't get the hook (existing panes keep it)",
                6_000,
            )
            self._refresh_rtk_button()
            return

        # Currently OFF → turn ON (confirm once, since it changes every pane's
        # Bash behavior). A project isn't required — the toggle is central — but
        # when one is active we also scrub any legacy per-project rtk hook.
        proj_name = active_project()[0] or "(no active project)"
        confirm = QMessageBox.question(
            self,
            "Enable rtk hook",
            (
                f"Enable the rtk PreToolUse Bash hook for cockpit panes.\n\n"
                f"Every Bash tool call in spawned panes will be auto-rewritten "
                f"with rtk (60-90% token savings on common dev output like git "
                f"diff, docker logs, npm ci, pytest, tsc).\n\n"
                f"This is a PERSONAL, central toggle — the hook is injected at "
                f"spawn time via --settings, so NO project files are written and "
                f"nothing lands in {proj_name}'s repo. Any legacy rtk hook a "
                f"previous cockpit build wrote into this project's "
                f".claude/settings.json is cleaned up.\n\n"
                f"You can turn it back off anytime from this same button."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        ok, msg = install_rtk(root)  # root=None is fine — central flag + no cleanup
        if ok:
            self._status.showMessage(f"rtk: {msg}", 6_000)
        else:
            QMessageBox.critical(self, "rtk enable failed", msg)
        self._refresh_rtk_button()
