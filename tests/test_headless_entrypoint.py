"""python -m agent_takkub.headless — boot-time error handling.

Covers the boot-failure -> events.log path and that HeadlessWindow.boot()
is actually invoked. The success-path test lets a real (but immediately
self-terminating) Qt event loop run — `QCoreApplication.exec` is a
C++-bound method PyQt6 won't let a plain monkeypatch override.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

# Imported at module level (collection time, before any fixture constructs a
# QApplication) — terminal_widget.py's `from PyQt6.QtWebEngineWidgets import
# QWebEngineView` must run before any QCoreApplication instance exists.
import agent_takkub.headless_window as hw_mod
from agent_takkub import config
from agent_takkub import headless as headless_mod


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def test_boot_failure_logs_and_returns_nonzero(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    events = tmp_path / "events.log"
    monkeypatch.setattr(config, "EVENTS_LOG", events, raising=False)
    monkeypatch.setattr(headless_mod, "EVENTS_LOG", events, raising=False)

    broken_window = MagicMock()
    broken_window.boot.side_effect = RuntimeError("port already in use")
    # main() imports HeadlessWindow lazily from .headless_window inside the
    # function body — patch the module it actually imports from.
    monkeypatch.setattr(hw_mod, "HeadlessWindow", MagicMock(return_value=broken_window))

    rc = headless_mod.main([])

    assert rc == 1
    assert events.exists()
    entry = json.loads(events.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["event"] == "headless_boot_failed"
    assert "port already in use" in entry["error"]


def test_boot_success_runs_event_loop(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    events = tmp_path / "events.log"
    monkeypatch.setattr(config, "EVENTS_LOG", events, raising=False)
    monkeypatch.setattr(headless_mod, "EVENTS_LOG", events, raising=False)

    def _boot_then_quit() -> int:
        # PyQt6's QCoreApplication.exec is a C++-bound method — not
        # monkeypatchable — so let a real (very short-lived) event loop run
        # and have boot() itself schedule the quit that ends it.
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, qapp.quit)
        return 5217

    ok_window = MagicMock()
    ok_window.boot.side_effect = _boot_then_quit
    ok_window._tabs = {"proj": object()}
    monkeypatch.setattr(hw_mod, "HeadlessWindow", MagicMock(return_value=ok_window))

    rc = headless_mod.main([])

    assert rc == 0
    ok_window.boot.assert_called_once()
