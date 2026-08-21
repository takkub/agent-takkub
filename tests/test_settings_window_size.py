"""Settings window must fit the screen it opens on (2026-08-21).

Reported from a teammate's laptop: the dialog opened at its hardcoded
1320x848 with a hardcoded 900x600 minimum, so on a smaller/scaled display the
60px footer — the only place "Save & Apply" lives — sat under the taskbar and
no amount of resizing could bring it back.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QSize

from agent_takkub import settings_window


@pytest.fixture
def small_screen(monkeypatch: pytest.MonkeyPatch) -> QSize:
    size = QSize(1024, 640)
    monkeypatch.setattr(settings_window.SettingsWindow, "_available_screen_size", lambda self: size)
    return size


class TestFitsTheScreen:
    def test_opens_no_larger_than_the_available_screen(self, small_screen: QSize) -> None:
        dlg = settings_window.SettingsWindow()
        try:
            assert dlg.width() <= small_screen.width()
            assert dlg.height() <= small_screen.height()
        finally:
            dlg.deleteLater()

    def test_minimum_never_exceeds_the_screen_so_the_footer_stays_reachable(
        self, small_screen: QSize
    ) -> None:
        """A minimum taller than the screen is the actual bug: the window
        cannot be shrunk to fit, so the footer stays off-screen forever."""
        dlg = settings_window.SettingsWindow()
        try:
            assert dlg.minimumHeight() <= small_screen.height()
            assert dlg.minimumWidth() <= small_screen.width()
        finally:
            dlg.deleteLater()

    def test_roomy_screen_keeps_the_original_size_and_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings_window.SettingsWindow,
            "_available_screen_size",
            lambda self: QSize(2560, 1440),
        )
        dlg = settings_window.SettingsWindow()
        try:
            assert (dlg.width(), dlg.height()) == (1320, 848)
            assert (dlg.minimumWidth(), dlg.minimumHeight()) == (900, 600)
        finally:
            dlg.deleteLater()

    def test_user_can_shrink_below_the_default_size(self, small_screen: QSize) -> None:
        """setSizeGripEnabled alone is not enough — the minimum has to allow
        it. Resizing to the floor must actually take effect."""
        dlg = settings_window.SettingsWindow()
        try:
            dlg.resize(dlg.minimumWidth(), dlg.minimumHeight())
            assert dlg.height() <= small_screen.height()
        finally:
            dlg.deleteLater()

    def test_size_hint_falls_back_when_qt_has_no_screen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Headless/offscreen platforms hand back no QScreen; the dialog must
        still build instead of dividing by a None geometry."""
        monkeypatch.setattr(
            settings_window.QGuiApplication, "primaryScreen", staticmethod(lambda: None)
        )
        monkeypatch.setattr(settings_window.SettingsWindow, "screen", lambda self: None)
        dlg = settings_window.SettingsWindow()
        try:
            assert dlg._available_screen_size() == QSize(1320, 848)
        finally:
            dlg.deleteLater()
