"""Tests for the Settings → General theme selector (#506): 3-way mode
(system/light/dark) at top level (not under ADVANCED), write-through persist,
and immediate live re-apply across open windows."""

from __future__ import annotations

import pytest

from agent_takkub import cockpit_theme, settings_window, theme_settings


@pytest.fixture(autouse=True)
def _restore_dark_variant():
    yield
    cockpit_theme.apply_variant("dark")


def _make_window() -> settings_window.SettingsWindow:
    return settings_window.SettingsWindow()


class TestGeneralViewWiring:
    def test_general_nav_entry_is_top_level_not_advanced(self) -> None:
        sections = {view: section for view, _label, section in settings_window._NAV_VIEWS}
        assert settings_window.VIEW_GENERAL in sections
        assert sections[settings_window.VIEW_GENERAL] not in settings_window._FOLDABLE_SECTIONS

    def test_general_view_never_joins_footer_save(self) -> None:
        """The theme applies+saves on change — the footer Save & Apply must
        not pretend to control it (same contract as the Core V2 pages)."""
        assert settings_window.VIEW_GENERAL in settings_window._NO_FOOTER_SAVE_VIEWS

    def test_stack_index_matches_view_constant(self) -> None:
        dlg = _make_window()
        try:
            dlg._stack.setCurrentIndex(settings_window.VIEW_GENERAL)
            assert dlg._stack.currentIndex() == settings_window.VIEW_GENERAL
            # the combo lives on that page and offers exactly the 3 modes
            assert dlg._theme_mode_combo.count() == 3
            offered = {
                dlg._theme_mode_combo.itemData(i) for i in range(dlg._theme_mode_combo.count())
            }
            assert offered == set(theme_settings.MODES)
        finally:
            dlg.deleteLater()

    def test_combo_preselects_the_persisted_mode(self) -> None:
        theme_settings.save("light")
        dlg = _make_window()
        try:
            assert dlg._theme_mode_combo.currentData() == "light"
        finally:
            dlg.deleteLater()
            theme_settings.save(theme_settings.DEFAULT_MODE)


class TestToggleAppliesAndPersists:
    def test_picking_light_persists_and_rebinds_tokens_immediately(self) -> None:
        theme_settings.save("dark")
        cockpit_theme.apply_variant("dark")
        dlg = _make_window()
        try:
            idx = dlg._theme_mode_combo.findData("light")
            dlg._theme_mode_combo.setCurrentIndex(idx)  # fires the change slot
            assert theme_settings.load() == "light"  # persisted (survives restart)
            assert cockpit_theme.current_variant() == "light"  # applied immediately
        finally:
            dlg.deleteLater()
            theme_settings.save(theme_settings.DEFAULT_MODE)

    def test_picking_system_resolves_via_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        theme_settings.save("dark")
        cockpit_theme.apply_variant("dark")
        monkeypatch.setattr(theme_settings, "detect_system_variant", lambda: "light")
        dlg = _make_window()
        try:
            dlg._theme_mode_combo.setCurrentIndex(dlg._theme_mode_combo.findData("system"))
            assert theme_settings.load() == "system"
            assert cockpit_theme.current_variant() == "light"
        finally:
            dlg.deleteLater()
            theme_settings.save(theme_settings.DEFAULT_MODE)

    def test_retheme_reapplies_stylesheet_with_new_tokens(self) -> None:
        dlg = _make_window()
        try:
            dark_qss = dlg.styleSheet()
            assert cockpit_theme.DARK_TOKENS["GROUND_WINDOW"] in dark_qss
            cockpit_theme.apply_variant("light")
            dlg.retheme()
            light_qss = dlg.styleSheet()
            assert str(cockpit_theme.LIGHT_TOKENS["GROUND_WINDOW"]) in light_qss
            assert str(cockpit_theme.DARK_TOKENS["GROUND_WINDOW"]) not in light_qss
        finally:
            dlg.deleteLater()

    def test_main_window_exposes_retheme_hook(self) -> None:
        """retheme_open_windows() reaches windows duck-typed by a `retheme`
        callable — MainWindow must keep one (base chrome re-applies live).
        Source-level check: importing main_window here trips QtWebEngine's
        must-import-before-QApplication rule under the session QApplication."""
        from pathlib import Path

        source = (Path(settings_window.__file__).parent / "main_window.py").read_text(
            encoding="utf-8"
        )
        assert "def retheme(self)" in source
        assert "def _apply_base_stylesheet(self)" in source

    def test_settings_management_window_exposes_retheme_hook(self) -> None:
        from agent_takkub.settings_management.window import SettingsManagementWindow

        assert callable(getattr(SettingsManagementWindow, "retheme", None))
