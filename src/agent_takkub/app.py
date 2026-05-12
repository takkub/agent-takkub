"""PyQt application entry point."""

from __future__ import annotations

import atexit
import os
import signal
import sys

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
        ]
    ),
)

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from .main_window import MainWindow


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
    app = QApplication(argv or sys.argv)
    app.setApplicationName("agent-takkub")
    # default font: prefer a Thai-capable sans
    f = QFont("Segoe UI", 10)
    app.setFont(f)
    w = MainWindow()
    _install_signal_handlers(w)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
