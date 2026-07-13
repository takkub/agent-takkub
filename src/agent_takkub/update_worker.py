"""Background worker and startup helper for cockpit self-update.

UpdateCheckWorker  — QRunnable that fetches origin/main + emits local_status()
                     without touching the Qt event loop. MainWindow wires a
                     5-minute QTimer to schedule repeated checks.

_try_silent_self_update — pre-UI fast-forward pull on startup. Re-execs when
                          dependencies are unchanged, or hands dependency sync
                          to a detached restart script. Called from app.main()
                          before QApplication is constructed; network failures
                          never prevent the cockpit from opening.
"""

from __future__ import annotations

import logging
import os
import sys

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from .config import REPO_ROOT, RUNTIME_DIR
from .update_helper import _git, fetch_remote, is_git_repo, local_status

logger = logging.getLogger(__name__)


def _safe_emit(signals: _WorkerSignals, payload: dict) -> None:
    """Emit `finished` while tolerating a receiver that was torn down mid-flight.

    These workers run on a QThreadPool thread and can outlive their creator: a
    cockpit restart/shutdown may delete the `_WorkerSignals` QObject (on the Qt
    thread) before the pool thread's git/network work returns. Calling `emit()`
    on a deleted C++ object raises `RuntimeError: wrapped C/C++ object of type
    _WorkerSignals has been deleted` — previously unhandled, which surfaced as a
    noisy boot.log traceback on the dying instance. If the receiver is gone there
    is nobody to deliver to anyway, so swallow exactly that RuntimeError."""
    try:
        signals.finished.emit(payload)
    except RuntimeError:
        logger.debug("update worker: receiver deleted before emit — dropping result")


# ── Worker signals carrier ────────────────────────────────────────────────────
# QRunnable cannot carry signals directly (it's not a QObject).  The
# canonical Qt pattern is a sibling QObject that lives on the creating
# thread; the worker holds a reference and calls emit() from the pool thread.


class _WorkerSignals(QObject):
    finished = pyqtSignal(dict)  # local_status() result dict


class UpdateCheckWorker(QRunnable):
    """Run fetch + local_status on a thread-pool thread.

    Usage (from MainWindow):
        worker = UpdateCheckWorker()
        worker.signals.finished.connect(self._on_update_check_done)
        QThreadPool.globalInstance().start(worker)
    """

    def __init__(self) -> None:
        super().__init__()
        self.signals = _WorkerSignals()

    def run(self) -> None:  # called by QThreadPool
        if not is_git_repo():
            _safe_emit(self.signals, {"not_repo": True, "ok": False})
            return
        try:
            fetch_remote(timeout=10.0)
        except Exception as exc:
            logger.debug("update worker: fetch_remote raised %s", exc)
        try:
            status = local_status()
        except Exception as exc:
            status = {"ok": False, "error": str(exc)}
        _safe_emit(self.signals, status)


class PullUpdateWorker(QRunnable):
    """Run the potentially network-bound update pull off the Qt main thread."""

    def __init__(self) -> None:
        super().__init__()
        self.signals = _WorkerSignals()

    def run(self) -> None:  # called by QThreadPool
        from .update_helper import pull_updates

        try:
            ok, message = pull_updates()
            result = {"ok": ok, "message": message}
        except Exception as exc:
            result = {"ok": False, "message": str(exc)}
        _safe_emit(self.signals, result)


class ClaudeUpdateCheckWorker(QRunnable):
    """Check Claude CLI version + (if newer) fetch changelog and run the
    compatibility analysis — all off the Qt thread, since `npm view`,
    the changelog GET, and especially the headless `claude -p` analysis
    can together take up to ~2-3 minutes.

    Emits one `finished(dict)` with keys:
        ok          bool   — False only on a fatal setup error
        error       str    — set when ok=False
        current     str|None
        latest      str|None
        has_update  bool
        analysis_ok bool   — whether the AI report succeeded
        analysis    str    — markdown report, or an error note
        changelog_ok bool

    Usage (from MainWindow):
        worker = ClaudeUpdateCheckWorker()
        worker.signals.finished.connect(self._on_claude_update_check_done)
        QThreadPool.globalInstance().start(worker)
    """

    def __init__(self) -> None:
        super().__init__()
        self.signals = _WorkerSignals()

    def run(self) -> None:  # called by QThreadPool
        from .claude_update import (
            analyze_compatibility,
            current_version,
            fetch_changelog,
            latest_version,
            slice_changelog,
        )

        result: dict = {
            "ok": True,
            "current": None,
            "latest": None,
            "has_update": False,
            "analysis_ok": False,
            "analysis": "",
            "changelog_ok": False,
            # issue auto-filing outcome (populated by _maybe_file_issue)
            "issue_action_required": False,
            "issue_number": None,
            "issue_url": None,
            "issue_skipped": False,  # True when a matching open issue already existed
            "issue_error": None,
        }
        try:
            cur = current_version()
            result["current"] = cur
            ok_latest, latest = latest_version()
            if not ok_latest:
                result.update(ok=False, error=f"เช็ค version ล่าสุดไม่ได้: {latest}")
                _safe_emit(self.signals, result)
                return
            result["latest"] = latest
            if cur is None:
                result.update(
                    ok=False, error="หา version ของ claude ปัจจุบันไม่ได้ (claude ไม่อยู่บน PATH?)"
                )
                _safe_emit(self.signals, result)
                return

            from .claude_update import compare_versions

            result["has_update"] = compare_versions(cur, latest) < 0
            if not result["has_update"]:
                _safe_emit(self.signals, result)
                return

            ok_cl, changelog = fetch_changelog()
            result["changelog_ok"] = ok_cl
            sliced = slice_changelog(changelog, cur) if ok_cl else ""
            ok_an, report = analyze_compatibility(cur, latest, sliced)
            result["analysis_ok"] = ok_an
            result["analysis"] = report
            # When the analysis says agent-takkub itself needs work, file a
            # GitHub issue so the user can come fix it later (their ask).
            if ok_an:
                self._maybe_file_issue(result, cur, latest, report)
        except Exception as exc:  # never let the pool thread die silently
            logger.debug("claude update worker raised %s", exc)
            result.update(ok=False, error=f"ตรวจสอบล้มเหลว: {exc}")
        _safe_emit(self.signals, result)

    @staticmethod
    def _maybe_file_issue(result: dict, cur: str, latest: str, report: str) -> None:
        """Parse the analyzer verdict; if action is required, open a GitHub
        issue against the agent-takkub repo (deduped by version range so
        repeated checks don't spam the tracker). Mutates `result` in place;
        never raises (gh hiccups become `issue_error`)."""
        from .claude_update import build_issue_body, build_issue_title, parse_verdict

        required, severity, suggested = parse_verdict(report)
        result["issue_action_required"] = required
        if not required:
            return

        title = build_issue_title(cur, latest, suggested)
        dedup_key = f"v{cur} → v{latest}"
        try:
            from . import issues

            # Dedup: skip if an open issue for this exact version range exists.
            try:
                existing = issues.list_issues(filter_open=True, cwd=str(REPO_ROOT))
            except Exception:
                existing = []
            for it in existing:
                if dedup_key in (it.get("title") or ""):
                    result["issue_skipped"] = True
                    result["issue_number"] = it.get("number")
                    result["issue_url"] = it.get("url")
                    return

            number, url = issues.new_issue(
                title,
                build_issue_body(cur, latest, report),
                severity=severity if severity in ("low", "med", "high") else "med",
                tags=["claude-update"],
                cockpit_bug=True,  # file against the agent-takkub repo, not the active project
            )
            result["issue_number"] = number
            result["issue_url"] = url
        except Exception as exc:
            logger.debug("claude update: issue filing failed %s", exc)
            result["issue_error"] = str(exc)


# ── Startup silent self-update (Layer C) ─────────────────────────────────────


def _log_startup_pull(event: str, **kw: object) -> None:
    """Append a one-line JSON record to runtime/startup_pull.log."""
    import json
    from datetime import datetime

    log_path = RUNTIME_DIR / "startup_pull.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def try_silent_self_update(*, timeout_fetch: float = 5.0, timeout_pull: float = 15.0) -> bool:
    """Best-effort pre-UI fast-forward pull.

    Returns True if a ff-pull succeeded and restart was invoked (the caller
    never reaches the return statement in that case).
    Returns False on any skip condition or error.

    Safety guards:
    - Not a git repo → skip
    - fetch timeout / error → skip (return False, don't crash)
    - dirty tree → skip (never stomp local edits)
    - already up-to-date → skip
    - ff-only fails (diverged) → skip
    """
    if not is_git_repo():
        return False
    dependency_sync_required = False
    try:
        ok, _msg = fetch_remote(timeout=timeout_fetch)
        if not ok:
            _log_startup_pull("fetch_failed", reason=_msg)
            return False
        st = local_status()
        if not st.get("ok"):
            _log_startup_pull("status_failed", error=st.get("error", ""))
            return False
        if not st.get("clean"):
            _log_startup_pull("skipped_dirty", files=st.get("dirty_files", []))
            return False
        behind = st.get("behind", 0)
        if behind == 0:
            return False  # already up to date — no log needed (common case)

        # Route every git call through update_helper._git so the pre-UI updater
        # inherits the same credential-prompt hardening as the in-app worker.
        # This is the path that produced the 1-thread startup husk: a bare
        # `git pull` here could hang past its timeout on a detached credential
        # helper (see update_helper.git_env), wedging the process before the
        # single-instance lock or QApplication ever ran.
        from_sha = (_git("rev-parse", "HEAD", timeout=5.0).stdout or "").strip()

        result = _git("pull", "--ff-only", "origin", "main", timeout=timeout_pull)
        if result.returncode != 0:
            _log_startup_pull(
                "pull_failed",
                returncode=result.returncode,
                stderr=(result.stderr or "").strip()[-200:],
            )
            return False

        to_sha = (_git("rev-parse", "HEAD", timeout=5.0).stdout or "").strip()
        _log_startup_pull("pulled", from_sha=from_sha, to_sha=to_sha, behind=behind)

        print(
            f"[takkub] auto-update: pulled {behind} commit(s) "
            f"({from_sha[:7]}->{to_sha[:7]}), restarting...",
            flush=True,
        )
        from .update_helper import pyproject_changed_in_pull

        if pyproject_changed_in_pull(from_sha, to_sha):
            dependency_sync_required = True
            import shutil
            import subprocess

            from .update_helper import build_pip_sync_script

            is_win = sys.platform == "win32"
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            log_path = RUNTIME_DIR / "pip_sync.log"
            script_path = RUNTIME_DIR / ("pip_sync.ps1" if is_win else "pip_sync.sh")
            script_path.write_text(
                build_pip_sync_script(
                    python_exe=sys.executable,
                    repo_root=str(REPO_ROOT),
                    log_path=str(log_path),
                    is_windows=is_win,
                    cockpit_pid=os.getpid(),
                ),
                encoding="utf-8",
            )
            if is_win:
                pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
                subprocess.Popen(
                    [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                    cwd=str(REPO_ROOT),
                    close_fds=True,
                    creationflags=0x00000008 | 0x00000200,
                )
            else:
                subprocess.Popen(
                    ["sh", str(script_path)],
                    cwd=str(REPO_ROOT),
                    close_fds=True,
                    start_new_session=True,
                )
            _log_startup_pull("pip_sync_spawned", script=str(script_path))
            raise SystemExit(0)

        # Re-exec into the newly fetched code when dependencies are unchanged.
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return True  # unreachable — os.execv replaces this process
    except Exception as exc:
        _log_startup_pull("exception", error=str(exc))
        if dependency_sync_required:
            print(f"[takkub] auto-update dependency sync failed: {exc}", flush=True)
        return False
