"""Headless entrypoint (#105 Phase B) — boots the orchestrator + cli_server +
remote-control server with NO display: no `MainWindow`, no `AgentPane`
widgets, no system tray. Every pane is a `HeadlessPane`
(`headless_pane.py`); the PWA (remote-control) is the only UI surface.

Usage:
    python -m agent_takkub.headless

Requires the same on-disk config as the desktop build (`projects.json`,
`~/.claude` / `~/.codex` credentials, `runtime/`) — see
`docs/guides/2026-07-11-headless-docker.md` for the Docker walkthrough.

Uses a bare `QCoreApplication`, not `QApplication`: headless mode never
constructs a `QWidget`, so no GUI platform plugin is needed — unlike the
test suite's offscreen `QApplication` fixture, this needs no
`QT_QPA_PLATFORM=offscreen`.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from datetime import datetime

from .config import EVENTS_LOG

_log = logging.getLogger(__name__)


def _log_boot_error(event: str, error_msg: str) -> None:
    """Best-effort events.log entry for a boot failure — the headless
    equivalent of `main_window._handle_cli_bind_error`'s step 1 (no
    `QMessageBox`: there is no display to show one on)."""
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "error": error_msg,
            },
            ensure_ascii=False,
        )
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    from PyQt6.QtCore import QCoreApplication

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # custom_roles must be loaded before any spawn — same boot-order
    # requirement app.py's _load_custom_roles() enforces on the desktop
    # build, so a `--role <custom>` assign works from a cold headless boot.
    try:
        from . import custom_roles

        custom_roles.load_and_register_all()
    except Exception:
        _log.exception("failed to load custom roles")

    # HeadlessWindow (which pulls in orchestrator -> agent_pane ->
    # terminal_widget -> QtWebEngineWidgets transitively) must be imported
    # before QCoreApplication is constructed — Qt requires
    # QtWebEngineWidgets to be imported ahead of any QCoreApplication
    # instance, same ordering app.py relies on via its module-level
    # `from .main_window import MainWindow`.
    from .headless_window import HeadlessWindow

    # Reuse an existing instance if one is already alive (e.g. under test,
    # where the process already holds a QApplication) — Qt aborts the
    # process if a second QCoreApplication is constructed.
    app = QCoreApplication.instance() or QCoreApplication(argv or sys.argv)
    app.setApplicationName("agent-takkub-headless")

    window = HeadlessWindow()

    def _handle_signal(signum: int, _frame: object) -> None:
        _log.info("signal %s received — shutting down", signum)
        window.shutdown()
        app.quit()

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _handle_signal)
        except (OSError, ValueError):
            pass

    try:
        port = window.boot()
    except Exception as exc:
        _log_boot_error("headless_boot_failed", str(exc))
        _log.exception("headless boot failed")
        return 1

    _log.info("agent-takkub headless — cli port %d, projects: %s", port, list(window._tabs))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
