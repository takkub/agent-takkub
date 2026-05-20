"""Background worker and startup helper for cockpit self-update.

UpdateCheckWorker  — QRunnable that fetches origin/main + emits local_status()
                     without touching the Qt event loop. MainWindow wires a
                     5-minute QTimer to schedule repeated checks.

_try_silent_self_update — pre-UI fast-forward pull on startup. Calls
                          os.execv to restart into the new code when a clean
                          ff-pull succeeds.  Called from app.main() before
                          QApplication is constructed; any failure is silently
                          swallowed so a network hiccup never prevents the
                          cockpit from opening.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import REPO_ROOT
from .update_helper import fetch_remote, is_git_repo, local_status

logger = logging.getLogger(__name__)


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
            self.signals.finished.emit({"not_repo": True, "ok": False})
            return
        try:
            fetch_remote(timeout=10.0)
        except Exception as exc:
            logger.debug("update worker: fetch_remote raised %s", exc)
        try:
            status = local_status()
        except Exception as exc:
            status = {"ok": False, "error": str(exc)}
        self.signals.finished.emit(status)


# ── Startup silent self-update (Layer C) ─────────────────────────────────────


def _log_startup_pull(event: str, **kw: object) -> None:
    """Append a one-line JSON record to runtime/startup_pull.log."""
    import json
    from datetime import datetime

    log_path = REPO_ROOT / "runtime" / "startup_pull.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def try_silent_self_update(*, timeout_fetch: float = 5.0, timeout_pull: float = 15.0) -> bool:
    """Best-effort pre-UI fast-forward pull.

    Returns True if a ff-pull succeeded and os.execv was invoked
    (the caller never reaches the return statement in that case).
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

        from_sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        from_sha = (from_sha_proc.stdout or "").strip()

        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only", "origin", "main"],
            capture_output=True,
            timeout=timeout_pull,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        if result.returncode != 0:
            _log_startup_pull(
                "pull_failed",
                returncode=result.returncode,
                stderr=(result.stderr or b"").decode("utf-8", errors="replace").strip()[-200:],
            )
            return False

        to_sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        to_sha = (to_sha_proc.stdout or "").strip()
        _log_startup_pull("pulled", from_sha=from_sha, to_sha=to_sha, behind=behind)

        print(
            f"[takkub] auto-update: pulled {behind} commit(s) "
            f"({from_sha[:7]}→{to_sha[:7]}), restarting…",
            flush=True,
        )
        # Re-exec into the newly fetched code.
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return True  # unreachable — os.execv replaces this process
    except Exception as exc:
        _log_startup_pull("exception", error=str(exc))
        return False
