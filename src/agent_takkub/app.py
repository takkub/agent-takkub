"""PyQt application entry point."""

from __future__ import annotations

import atexit
import faulthandler
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

# Boot-time crash dump. pythonw.exe has no console, so a segfault or
# uncaught exception during MainWindow init looks like a silent
# disappearance. Route faulthandler + a small textual trace into
# runtime/boot.log so we can read it post-mortem.
_BOOT_LOG = Path(__file__).resolve().parents[2] / "runtime" / "boot.log"
try:
    _BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    _BOOT_LOG_FH = _BOOT_LOG.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(_BOOT_LOG_FH)
    _BOOT_LOG_FH.write(f"\n--- boot {os.getpid()} ---\n")
except Exception:
    _BOOT_LOG_FH = None


def _boot_log(msg: str) -> None:
    if _BOOT_LOG_FH:
        try:
            _BOOT_LOG_FH.write(msg + "\n")
            _BOOT_LOG_FH.flush()
        except Exception:
            pass


# Chromium throttles background timers, RAF and rendering for views that
# aren't the foreground tab. Because we host many xterm.js panes in one
# window, only one is "focused" at a time and the rest get paint-suppressed
# — output reaches xterm.js but the DOM doesn't repaint until the user
# pokes the view. Flip the throttles off before QtWebEngine boots.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    " ".join(
        [
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion",
            # Cap renderer process count so dozens of panes (multi-project
            # tabs) don't each spawn a fresh Chromium renderer at ~150 MB
            # baseline. With this flag Chromium reuses renderer processes
            # across views past the limit, trading isolation for memory.
            # 4 is enough to keep paint pipelines responsive without
            # ballooning RAM.
            "--renderer-process-limit=4",
            # Force software compositing. With many xterm.js WebEngine views
            # (2+ project tabs) the shared GPU process gets overwhelmed and
            # crashes — every view goes blank/white and the window stops
            # responding (the classic QtWebEngine-on-Windows "white screen").
            # A text terminal needs no GPU, so software rendering trades a
            # little CPU for not having a GPU process that can take the whole
            # UI down. See docs/cockpit-freeze-rca-2026-05-29.md.
            "--disable-gpu",
            "--disable-gpu-compositing",
        ]
    ),
)

from PyQt6.QtCore import QLockFile  # noqa: E402
from PyQt6.QtGui import QFont  # noqa: E402 — PyQt must import after env setup above
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from .main_window import MainWindow  # noqa: E402
from .update_worker import try_silent_self_update  # noqa: E402

# Temp-dir lock file prevents two cockpit processes from co-existing.
# OS-level advisory lock: automatically released when the process exits
# (even on force-kill), so a crashed cockpit never permanently blocks restart.
_LOCK_PATH = str(Path(tempfile.gettempdir()) / "agent-takkub-cockpit.lock")
_instance_lock: QLockFile | None = None  # module-level ref keeps GC from releasing the lock

# Dead-man watchdog constants.
# 30-second threshold: normal Qt operations (modal dialogs, large snapshot
# writes) resolve in well under 10 s. Legitimate slow paths at startup don't
# exceed ~5 s. 30 s is conservative enough to never fire on transient slowness
# yet fast enough to catch the 21-minute zombie observed in issue #34.
_WATCHDOG_TIMEOUT_S = 30.0
_WATCHDOG_POLL_S = 5.0


def _watchdog_should_exit(heartbeat_ts: float, now: float, timeout_s: float) -> bool:
    """Pure helper: True when the main-thread heartbeat has been stale too long.

    Extracted for unit-testability — the daemon thread calls this in a loop.
    """
    return (now - heartbeat_ts) > timeout_s


def _start_deadman_watchdog(window: MainWindow, _stop: threading.Event | None = None) -> None:
    """Start a background daemon that kills the process when the Qt main thread wedges.

    MainWindow._heartbeat_timer ticks _heartbeat_ts every ~1 s from the Qt event
    loop. If the heartbeat stops advancing for _WATCHDOG_TIMEOUT_S seconds the
    main thread is blocked — a busy-loop, a blocking subprocess.run call on the
    Qt thread, or a deadlock. We call os._exit() rather than sys.exit() because
    sys.exit() raises SystemExit which needs the main thread to handle it, and
    the main thread is exactly what we cannot reach when it is wedged.

    _stop is an optional threading.Event used only in tests to halt the daemon
    cleanly without killing the process.
    """

    def _run() -> None:
        while not (_stop is not None and _stop.is_set()):
            time.sleep(_WATCHDOG_POLL_S)
            if _stop is not None and _stop.is_set():
                return
            now = time.monotonic()
            if _watchdog_should_exit(window._heartbeat_ts, now, _WATCHDOG_TIMEOUT_S):
                age = now - window._heartbeat_ts
                _boot_log(
                    f"[watchdog] main thread wedged for {age:.0f}s"
                    " — terminating children then os._exit(1)"
                )
                # Best-effort: terminate child sessions so they don't become
                # orphans after os._exit bypasses atexit/_kill_all.
                # pane.session.terminate() is OS-level (TerminateProcess) and
                # safe to call from a daemon thread; wrap every access in
                # try/except to guard against races with teardown on the main
                # thread (e.g. a pane being removed while we iterate).
                try:
                    for pane in list(window.orch.panes.values()):
                        if pane.session is not None:
                            try:
                                pane.mark_expected_exit()
                                pane.session.terminate()
                            except Exception:
                                pass
                except Exception:
                    pass
                os._exit(1)

    t = threading.Thread(target=_run, daemon=True, name="cockpit-deadman")
    t.start()


def _install_signal_handlers(window: MainWindow) -> None:
    """Ensure spawned claude/winpty-agent children die with us, even when the
    Qt window crashes or the process is killed externally (Ctrl+Break, parent
    terminal close, OOM). closeEvent only fires on graceful close — these
    hooks cover the rest."""

    def _kill_all() -> None:
        for pane in list(window.orch.panes.values()):
            if pane.session is not None:
                try:
                    pane.mark_expected_exit()
                    pane.session.terminate()
                except Exception:
                    pass
        try:
            window.cli.close()
        except Exception:
            pass

    atexit.register(_kill_all)

    # SIGINT (Ctrl+C in launching terminal) + SIGTERM. Windows raises
    # SIGBREAK on Ctrl+Break — handle it the same way.
    def _on_signal(_sig: int, _frame: object | None) -> None:
        _kill_all()
        # let Qt clean up gracefully too
        QApplication.quit()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                # not all signals are settable on all platforms
                pass


def main(argv: list[str] | None = None) -> int:
    # Layer C: silent fast-forward pull before UI starts.  If a pull
    # succeeds, os.execv re-execs into the new code — execution never
    # reaches the next line.  Any failure returns False silently.
    try_silent_self_update()

    app = QApplication(argv or sys.argv)
    app.setApplicationName("agent-takkub")
    f = QFont("Segoe UI", 10)
    app.setFont(f)

    # Single-instance guard: refuse to open a second cockpit window.
    # tryLock(100) waits at most 100 ms so startup delay is imperceptible.
    global _instance_lock
    _instance_lock = QLockFile(_LOCK_PATH)
    if not _instance_lock.tryLock(100):
        _boot_log(f"[single-instance] lock held — refusing duplicate start (pid={os.getpid()})")
        QMessageBox.warning(
            None,
            "agent-takkub already running",
            "A cockpit window is already open.\n\n"
            "Close the existing window before starting a new one.\n\n"
            "If the old window is unresponsive, use Task Manager to end\n"
            "the 'pythonw.exe' process and try again.",
        )
        return 1

    w = MainWindow()
    _install_signal_handlers(w)
    _start_deadman_watchdog(w)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
