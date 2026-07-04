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


def _log_unhandled(exc_type, exc_value, exc_tb, *, source: str) -> None:
    import traceback

    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _boot_log(f"\n--- UNHANDLED EXCEPTION ({source}) pid={os.getpid()} ---\n{tb}")
    try:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(tb)
    except Exception:
        pass


def _install_exception_guard() -> None:
    """Stop PyQt6 from hard-aborting the process on a slot exception.

    When a Python exception escapes a slot / virtual / eventFilter that Qt
    invoked from C++, PyQt6's *default* behaviour is to call qFatal(), which
    fast-fails the whole process: Qt6Core __fastfail, SEH code 0xc0000409,
    exception subcode 0x7 (FAST_FAIL_FATAL_APP_EXIT). pythonw.exe has no
    console, so the Python traceback is lost and the cockpit just "vanishes" —
    this is the hard-crash seen on pane close (and the reason disabling the
    office-room / deferring the teardown didn't help: the crash is whatever
    slot raised, not those code paths).

    Installing a NON-default sys.excepthook flips PyQt6 to route the exception
    here and *continue* the event loop instead of aborting, and records the
    real traceback in boot.log so the offending slot can be found and fixed.
    Belt-and-suspenders: cover worker threads and unraisable (GC/__del__) paths
    too.
    """

    def _hook(exc_type, exc_value, exc_tb):
        _log_unhandled(exc_type, exc_value, exc_tb, source="sys.excepthook")

    sys.excepthook = _hook

    def _thread_hook(args):
        name = args.thread.name if args.thread is not None else "?"
        _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback, source=f"thread:{name}")

    threading.excepthook = _thread_hook

    if hasattr(sys, "unraisablehook"):

        def _unraisable(unr):
            _log_unhandled(
                type(unr.exc_value),
                unr.exc_value,
                unr.exc_traceback,
                source="unraisable",
            )

        sys.unraisablehook = _unraisable


# Install immediately so exceptions during MainWindow construction (slots fired
# from C++ before the event loop even starts) are caught rather than aborting.
_install_exception_guard()


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
    # CalculateNativeWinOcclusion: classic QtWebEngine white-screen workaround.
    # HardwareMediaKeyHandling / GlobalMediaControls: on Windows these install a
    #   low-level keyboard hook (WH_KEYBOARD_LL via Chromium's MediaKeysListener)
    #   that sits in the system hook chain and can swallow the Windows/Super key
    #   while the cockpit window is focused — the Start menu then appears dead
    #   even though the key works fine before the app launches. The cockpit never
    #   plays media, so disabling these removes the hook with zero functional
    #   loss. (Multiple features go in ONE --disable-features= flag, comma-joined;
    #   a second flag would override the first.)
    "--disable-features=CalculateNativeWinOcclusion,HardwareMediaKeyHandling,GlobalMediaControls",
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


def _should_allow_multi() -> bool:
    """True when TAKKUB_ALLOW_MULTI=1 is set — dev/test multi-instance mode."""
    return os.environ.get("TAKKUB_ALLOW_MULTI", "").strip() == "1"


def _wait_predecessor_exit(
    lock,
    timeout_s: float = 20.0,
    poll_s: float = 0.25,
    _sleep=None,
    _clock=None,
) -> bool:
    """Poll the single-instance lock until the exiting predecessor releases it.

    Used only on the `takkub restart` successor path (TAKKUB_RESTART_SUCCESSOR):
    the old cockpit is shutting down GRACEFULLY, so killing it or showing the
    "already running" dialog would be wrong — WebEngine teardown just takes a
    few seconds. Returns True once the lock is acquired, False on timeout
    (caller falls through to the normal auto-kill/dialog path).
    `_sleep`/`_clock` are injectable for tests.
    """
    sleep = _sleep or time.sleep
    clock = _clock or time.monotonic
    deadline = clock() + timeout_s
    while clock() < deadline:
        sleep(poll_s)
        if lock.tryLock(100):
            return True
    return False


# When running in multi-instance mode, steer cli_server to a per-PID port file
# so the second instance doesn't overwrite the main instance's runtime/port,
# and panes (which inherit this env) auto-connect to the right cockpit.
# Must be set BEFORE config is imported (config reads PORT_FILE at call time).
if _should_allow_multi():
    _multi_port_file = Path(tempfile.gettempdir()) / f"agent-takkub-port.{os.getpid()}"
    os.environ.setdefault("TAKKUB_PORT_FILE", str(_multi_port_file))


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
    """Ensure spawned claude/pty children die with us, and that every pane's
    PTY reader/writer QThread is stopped BEFORE Qt tears the app down — even on
    the quit paths that bypass ``closeEvent``.

    ``closeEvent`` only fires when a window is actually closed (the red X / Alt+F4).
    macOS ⌘Q, the app-menu Quit, ``QApplication.quit()`` and a SIGTERM all end the
    event loop WITHOUT a ``closeEvent`` — leaving each pane's still-running QThread
    to be destroyed by Qt, which is a fatal error ("QThread: Destroyed while thread
    is still running" → SIGABRT). Connecting the same teardown to ``aboutToQuit``
    (emitted for every quit path before QObject destruction) closes that gap, and
    atexit + signal handlers cover hard kills / crashes."""

    def _kill_all() -> None:
        # Walk EVERY project namespace, not just the active tab, so background
        # tabs' panes are torn down too (mirrors MainWindow.closeEvent).
        try:
            projects = list(window.orch._panes_by_project.values())
        except Exception:
            projects = []
        for project_panes in projects:
            for pane in list(project_panes.values()):
                if pane.session is not None:
                    try:
                        pane.mark_expected_exit()
                        # wait=True: finish taskkill /T inline before exit so the
                        # process tree can't be orphaned by a half-run daemon.
                        pane.session.terminate(wait=True)
                    except Exception:
                        pass
        try:
            window.cli.close()
        except Exception:
            pass

    atexit.register(_kill_all)

    # Primary graceful path for ⌘Q / menu-Quit / quit(): runs while the QThreads
    # are still joinable, before Qt destroys them. terminate() is idempotent, so
    # this is safe even when closeEvent already ran.
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(_kill_all)

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
    # Segoe UI ships on Windows; on macOS it is absent, which triggers a
    # costly font-alias scan + fallback at startup. Pick the native UI font
    # per platform (both branches present so neither OS is left out).
    if sys.platform == "win32":
        _ui_family = "Segoe UI"
    elif sys.platform == "darwin":
        _ui_family = "Helvetica Neue"
    else:
        _ui_family = "sans-serif"
    f = QFont(_ui_family, 10)
    app.setFont(f)

    # Single-instance guard: refuse to open a second cockpit window.
    # tryLock(100) waits at most 100 ms so startup delay is imperceptible.
    # TAKKUB_ALLOW_MULTI=1 skips the lock entirely for dev/test multi-instance runs.
    global _instance_lock
    if _should_allow_multi():
        _boot_log(f"[single-instance] TAKKUB_ALLOW_MULTI=1 — skipping lock (pid={os.getpid()})")
    else:
        _instance_lock = QLockFile(_LOCK_PATH)
    if not _should_allow_multi() and not _instance_lock.tryLock(100):
        # `takkub restart` successor: the predecessor is EXPECTED to still be
        # exiting (WebEngine teardown takes seconds) — wait for it to release
        # the lock instead of racing into auto-kill / the "already running"
        # dialog. Falls through to the normal path only if the wait times out.
        if os.environ.pop("TAKKUB_RESTART_SUCCESSOR", None) == "1" and _wait_predecessor_exit(
            _instance_lock
        ):
            _boot_log("[single-instance] restart successor — predecessor exited, lock acquired")
            w = MainWindow()
            _install_signal_handlers(w)
            _start_deadman_watchdog(w)
            w.show()
            return app.exec()
        _boot_log(
            f"[single-instance] lock held — attempting auto-kill of stale process (pid={os.getpid()})"
        )
        # Auto-kill the existing process if it's still running.
        # getLockInfo() returns (success: bool, pid: int, hostname: str, appname: str).
        lock_info = _instance_lock.getLockInfo()
        success = lock_info[0] if lock_info else False
        old_pid = lock_info[1] if (lock_info and len(lock_info) > 1) else 0
        if success and old_pid > 0 and old_pid != os.getpid():
            _boot_log(f"[single-instance] killing old process pid={old_pid}")
            try:
                import psutil

                proc = psutil.Process(old_pid)
                # Kill the whole process tree (cockpit + spawned panes/CLIs).
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                proc.kill()
                proc.wait(timeout=3)  # wait for process to die
                _boot_log("[single-instance] old process killed, retrying lock")
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.TimeoutExpired,
                Exception,
            ) as e:
                _boot_log(f"[single-instance] kill failed: {e}")
                # Fall through to dialog below.
            # Retry lock after kill.
            time.sleep(0.1)  # small delay for OS to release lock
            if _instance_lock.tryLock(100):
                _boot_log("[single-instance] lock acquired after auto-kill")
            else:
                _boot_log("[single-instance] lock still held after kill — showing dialog")
                QMessageBox.warning(
                    None,
                    "agent-takkub already running",
                    "A cockpit window is already open.\n\n"
                    "Close the existing window before starting a new one.\n\n"
                    "If the old window is unresponsive, use Task Manager to end\n"
                    "the 'pythonw.exe' process and try again.",
                )
                return 1
        else:
            _boot_log(
                f"[single-instance] cannot read PID from lock (success={success}, pid={old_pid}) — showing dialog"
            )
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
