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
    # boot.log is append-only across every launch. Cap it so stale faulthandler
    # dumps from earlier builds don't accumulate forever — a 169 KB log of
    # historical "wedged 1s" lines from an old watchdog actively misled the
    # 2026-06-04 freeze post-mortem. Keep the tail when it grows past 256 KB.
    try:
        if _BOOT_LOG.exists() and _BOOT_LOG.stat().st_size > 256 * 1024:
            _tail = _BOOT_LOG.read_bytes()[-32 * 1024 :]
            _BOOT_LOG.write_bytes(b"--- (boot.log rotated, tail kept) ---\n" + _tail)
    except Exception:
        pass
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
# Software vs hardware compositing.
#
# History: GPU was force-disabled (--disable-gpu) because the *shared* Chromium
# GPU process could get overwhelmed with many xterm.js views (2+ project tabs)
# and crash — every view goes blank/white and the window stops responding (the
# classic QtWebEngine-on-Windows "white screen"; see
# docs/cockpit-freeze-rca-2026-05-29.md). The trade was "a little CPU" for GPU
# stability.
#
# In practice that "little CPU" is not little: software rasterising every pane
# pegs the CPU and makes the whole UI feel janky/stuttery even on a fast machine
# with a discrete GPU and plenty of RAM — exactly the case where hardware
# compositing is dramatically smoother. So the default is now **hardware GPU
# ON**. If the GPU process ever destabilises on a given machine (white screen),
# set TAKKUB_FORCE_SOFTWARE_GPU=1 to fall back to the old software path WITHOUT
# editing code.
_force_software_gpu = os.environ.get("TAKKUB_FORCE_SOFTWARE_GPU", "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
)

_chromium_flags = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=CalculateNativeWinOcclusion",
    # Cap renderer process count so dozens of panes (multi-project tabs) don't
    # each spawn a fresh Chromium renderer at ~150 MB baseline. With this flag
    # Chromium reuses renderer processes across views past the limit, trading
    # isolation for memory.
    "--renderer-process-limit=4",
]

if _force_software_gpu:
    # Legacy safe path: no GPU process at all (cannot take the UI down), at the
    # cost of CPU-bound software rasterisation.
    _chromium_flags += ["--disable-gpu", "--disable-gpu-compositing"]
else:
    # Hardware compositing + GPU rasterisation → smooth xterm.js scroll/paint
    # and the CPU freed from rasterising every pane. --ignore-gpu-blocklist
    # forces acceleration on even if Chromium's driver blocklist is overly
    # cautious (common on otherwise-capable Windows GPUs).
    _chromium_flags += [
        "--enable-gpu-rasterization",
        "--ignore-gpu-blocklist",
    ]

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", " ".join(_chromium_flags))

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
# Poll at 250 ms so sub-second stalls are sampled (matches the 250 ms heartbeat).
_WATCHDOG_POLL_S = float(os.environ.get("TAKKUB_WATCHDOG_POLL_S", "0.25"))
# Soft-stall threshold: the main thread is hung for a couple seconds (UI freezes)
# but recovers before the 30 s hard kill. These transient freezes — e.g. a pane
# spawn that briefly blocks the Qt thread — never reach _WATCHDOG_TIMEOUT_S, so
# without a separate capture they leave no stack. On a soft stall we dump the
# main-thread stack to boot.log WITHOUT killing the process (once per episode).
# Lowered 3.0 → 1.5 so the shorter spawn freezes that started this investigation
# also leave a stack.
_WATCHDOG_SOFT_STALL_S = float(os.environ.get("TAKKUB_WATCHDOG_SOFT_STALL_S", "1.5"))
# Stall-log threshold: the watchdog records a structured `main_thread_stall`
# event to events.log for ANY freeze whose peak exceeds this — cheaper than a
# stack dump, and queryable. Set just above the normal age ceiling (~0.25–0.35 s
# with a 250 ms heartbeat) so routine operation never false-fires. Each event
# carries the peak duration and whether a pane spawn was in flight during the
# episode — the signal that confirms (or refutes) the spawn-induced-freeze
# hypothesis without guessing.
_WATCHDOG_STALL_LOG_S = float(os.environ.get("TAKKUB_STALL_LOG_S", "0.75"))


def _watchdog_should_exit(heartbeat_ts: float, now: float, timeout_s: float) -> bool:
    """Pure helper: True when the main-thread heartbeat has been stale too long.

    Extracted for unit-testability — the daemon thread calls this in a loop.
    """
    return (now - heartbeat_ts) > timeout_s


class _StallTracker:
    """Episode tracker for sub-hard-kill main-thread stalls (pure, testable).

    Fed one (age, spawn_in_progress) sample per watchdog poll. While `age`
    stays above the log threshold the episode is "active": it tracks the peak
    age and latches whether a pane spawn was ever in flight during it (latching
    survives a spawn that finishes mid-episode, which a single instantaneous
    read would miss). When `age` falls back below the threshold the episode
    ends and `update()` returns a record dict to log exactly once; otherwise
    returns None.
    """

    def __init__(self, log_threshold_s: float) -> None:
        self._th = log_threshold_s
        self._active = False
        self._peak = 0.0
        self._saw_spawn = False

    def update(self, age: float, spawn_in_progress: bool) -> dict | None:
        if age > self._th:
            self._active = True
            self._peak = max(self._peak, age)
            self._saw_spawn = self._saw_spawn or bool(spawn_in_progress)
            return None
        if self._active:
            rec = {
                "duration_ms": round(self._peak * 1000),
                "spawn_in_progress": self._saw_spawn,
            }
            self._active = False
            self._peak = 0.0
            self._saw_spawn = False
            return rec
        return None


def _dump_main_stack(header: str) -> None:
    """Write *header* + a faulthandler all-threads dump to boot.log.

    Called from the watchdog daemon thread; the dump includes the (possibly
    wedged) main thread's frames, which is the whole point. Best-effort.
    """
    try:
        if _BOOT_LOG_FH:
            _BOOT_LOG_FH.write(header + "\n")
            _BOOT_LOG_FH.flush()
            faulthandler.dump_traceback(file=_BOOT_LOG_FH, all_threads=True)
            _BOOT_LOG_FH.flush()
    except Exception:
        pass


def _start_deadman_watchdog(window: MainWindow, _stop: threading.Event | None = None) -> None:
    """Start a background daemon that LOGS (never kills) Qt main-thread wedges.

    MainWindow._heartbeat_timer ticks _heartbeat_ts every ~1 s from the Qt event
    loop. If the heartbeat stops advancing for _WATCHDOG_TIMEOUT_S seconds the
    main thread is blocked — a busy-loop, a blocking subprocess.run call on the
    Qt thread, or a deadlock.

    HARD-KILL DISABLED (user request 2026-06-10): the old behaviour called
    os._exit(1) on a wedge, which nuked the whole cockpit on resume-from-sleep
    (the heartbeat is frozen during system suspend, so on wake the age is huge
    and the watchdog fired instantly) and on any transient native wedge. Per
    user: never auto-kill — if the UI wedges they close it manually. The daemon
    is kept purely for diagnostics: it dumps the wedged main-thread stack to
    boot.log so freezes stay debuggable, but it no longer terminates children,
    snapshots, or exits.

    _stop is an optional threading.Event used only in tests to halt the daemon
    cleanly.
    """

    def _run() -> None:
        soft_dumped = False
        hard_dumped = False
        stall_tracker = _StallTracker(_WATCHDOG_STALL_LOG_S)
        while not (_stop is not None and _stop.is_set()):
            time.sleep(_WATCHDOG_POLL_S)
            if _stop is not None and _stop.is_set():
                return
            now = time.monotonic()
            age = now - window._heartbeat_ts
            # Soft stall: UI hung a few seconds but not yet at the hard kill.
            # Capture the main-thread stack once per episode (re-arm on recovery)
            # so transient spawn freezes — which recover before 30 s — still
            # leave a diagnosable stack. Does NOT kill the process.
            if not _watchdog_should_exit(window._heartbeat_ts, now, _WATCHDOG_TIMEOUT_S):
                if age > _WATCHDOG_SOFT_STALL_S and not soft_dumped:
                    _dump_main_stack(f"[watchdog] SOFT stall {age:.1f}s — main-thread stack:")
                    soft_dumped = True
                elif age <= _WATCHDOG_SOFT_STALL_S:
                    soft_dumped = False
                # Structured stall record (cheaper than a stack dump, queryable
                # from events.log). Latch whether a pane spawn was in flight so
                # we can confirm/refute the spawn-induced-freeze hypothesis. The
                # orchestrator's `_spawn_in_progress` bool is read cross-thread
                # (atomic under the GIL); never mutate orchestrator state here.
                spawn_active = bool(
                    getattr(getattr(window, "orch", None), "_spawn_in_progress", False)
                )
                rec = stall_tracker.update(age, spawn_active)
                if rec is not None:
                    try:
                        from .orchestrator import _log_event

                        _log_event("main_thread_stall", **rec)
                    except Exception:
                        pass
            # Wedge detected. DIAGNOSTIC ONLY — never kill the process (see the
            # docstring: hard-kill disabled per user request). Dump the wedged
            # main-thread stack to boot.log once per episode (re-arm when the
            # heartbeat recovers) so the freeze stays debuggable, then leave the
            # process completely alone. If it stays wedged the user closes it.
            if _watchdog_should_exit(window._heartbeat_ts, now, _WATCHDOG_TIMEOUT_S):
                age = now - window._heartbeat_ts
                if not hard_dumped:
                    _boot_log(
                        f"[watchdog] main thread wedged for {age:.0f}s"
                        " — hard-kill disabled, leaving process alive"
                    )
                    _dump_main_stack(f"[watchdog] main-thread stack at wedge (age {age:.0f}s):")
                    hard_dumped = True
            else:
                hard_dumped = False

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
