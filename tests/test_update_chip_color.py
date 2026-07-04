"""Tests for the 'Update via npm' chip colour on installed (non-git) cockpits.

Regression: an npm/pip-installed cockpit hits the `not_repo` branch of
`_refresh_update_button`, which used to be a hardcoded green chip — it never
queried the npm registry, so it stayed green even when a newer build was
published. These tests pin the new behaviour: the chip flips to the blue
"Update available" style when the cached npm check reports a newer version, and
stays green when up-to-date or unchecked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

import agent_takkub.main_window as mw_mod


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_window_stub() -> mw_mod.MainWindow:
    """MainWindow-like object with just the attributes the update chip touches."""
    with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
        win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)
    win._btn_update = MagicMock()
    win._status = MagicMock()
    win._refresh_version_label = MagicMock()
    win._update_status_cache = {"not_repo": True}
    win._npm_update_cache = None
    win._npm_check_busy = False
    return win


def _style(win) -> str:
    """The last stylesheet string pushed to the chip."""
    return win._btn_update.setStyleSheet.call_args[0][0]


def _text(win) -> str:
    return win._btn_update.setText.call_args[0][0]


class TestNpmChipColour:
    def test_blue_when_registry_has_newer_version(self, qapp) -> None:
        win = _make_window_stub()
        win._npm_update_cache = {"ok": True, "current": "1.0.9", "latest": "1.0.10"}
        with patch("agent_takkub.config.is_installed_package", return_value=True):
            win._refresh_update_button()
        assert "Update available (v1.0.10)" in _text(win)
        # Blue "stands out" palette (same as the git behind-state).
        assert "#93c5fd" in _style(win)

    def test_green_when_up_to_date(self, qapp) -> None:
        win = _make_window_stub()
        win._npm_update_cache = {"ok": True, "current": "1.0.10", "latest": "1.0.10"}
        with patch("agent_takkub.config.is_installed_package", return_value=True):
            win._refresh_update_button()
        assert _text(win) == "🔄 Update via npm"
        assert "#052e16" in _style(win)  # neutral green background

    def test_green_before_first_check(self, qapp) -> None:
        win = _make_window_stub()
        win._npm_update_cache = None  # no registry poll completed yet
        with patch("agent_takkub.config.is_installed_package", return_value=True):
            win._refresh_update_button()
        assert _text(win) == "🔄 Update via npm"
        assert "#052e16" in _style(win)

    def test_green_when_check_failed(self, qapp) -> None:
        # A failed registry check must not false-alarm as "update available".
        win = _make_window_stub()
        win._npm_update_cache = {"ok": False, "current": "1.0.10", "latest": ""}
        with patch("agent_takkub.config.is_installed_package", return_value=True):
            win._refresh_update_button()
        assert _text(win) == "🔄 Update via npm"
        assert "#052e16" in _style(win)


class TestNpmCheckWiring:
    def test_check_done_caches_and_refreshes(self, qapp) -> None:
        win = _make_window_stub()
        win._npm_check_busy = True
        win._refresh_update_button = MagicMock()
        win._on_npm_update_check_done(True, "1.0.9", "1.0.10", "")
        assert win._npm_update_cache == {"ok": True, "current": "1.0.9", "latest": "1.0.10"}
        assert win._npm_check_busy is False
        win._refresh_update_button.assert_called_once()

    def test_failed_check_keeps_last_good_cache(self, qapp) -> None:
        win = _make_window_stub()
        win._npm_update_cache = {"ok": True, "current": "1.0.9", "latest": "1.0.10"}
        win._refresh_update_button = MagicMock()
        win._on_npm_update_check_done(False, "1.0.9", "", "registry timeout")
        # Last good (update-available) cache preserved — chip doesn't regress.
        assert win._npm_update_cache == {"ok": True, "current": "1.0.9", "latest": "1.0.10"}

    def test_not_repo_installed_triggers_npm_check(self, qapp) -> None:
        win = _make_window_stub()
        win._update_status_cache = None
        win._update_worker_busy = True
        win._schedule_npm_update_check = MagicMock()
        win._refresh_update_button = MagicMock()
        with patch("agent_takkub.config.is_installed_package", return_value=True):
            win._on_update_check_done({"not_repo": True, "ok": False})
        win._schedule_npm_update_check.assert_called_once()

    def test_not_repo_source_checkout_skips_npm_check(self, qapp) -> None:
        # A dev source checkout that somehow reports not_repo must NOT run the
        # npm-registry path (updates come from git there).
        win = _make_window_stub()
        win._update_status_cache = None
        win._update_worker_busy = True
        win._schedule_npm_update_check = MagicMock()
        win._refresh_update_button = MagicMock()
        with patch("agent_takkub.config.is_installed_package", return_value=False):
            win._on_update_check_done({"not_repo": True, "ok": False})
        win._schedule_npm_update_check.assert_not_called()
