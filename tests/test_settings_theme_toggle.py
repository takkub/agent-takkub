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


class TestRethemeReachesInlineStyledChildren:
    """#505 review M9: `retheme()`'s parent QSS cascade never reaches a
    child built with its own inline ``setStyleSheet(f"...{color}...")`` —
    Accounts' account-name labels and Usage's card labels bake a literal
    color at construction time. `retheme()` must rebuild those specific
    children from cached state (no credential re-read, no re-query)."""

    def test_accounts_card_label_repaints_on_live_theme_switch(self) -> None:
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        try:
            QThreadPool.globalInstance().waitForDone(5_000)
            app = QApplication.instance()
            for _ in range(10):
                app.processEvents()
            assert dlg._accounts_rows_cache is not None  # background refresh landed

            provider_panel = dlg._accounts_rows_box.itemAt(0).widget()
            name_lbl = next(
                w
                for w in provider_panel.findChildren(settings_window.QLabel)
                if cockpit_theme.DARK_TOKENS["TEXT_PRIMARY_ALT"] in w.styleSheet()
                or cockpit_theme.DARK_TOKENS["TEXT_MUTED"] in w.styleSheet()
            )
            assert cockpit_theme.LIGHT_TOKENS["TEXT_PRIMARY_ALT"] not in name_lbl.styleSheet()

            cockpit_theme.apply_variant("light")
            dlg.retheme()

            provider_panel = dlg._accounts_rows_box.itemAt(0).widget()
            name_lbl = next(
                w
                for w in provider_panel.findChildren(settings_window.QLabel)
                if cockpit_theme.LIGHT_TOKENS["TEXT_PRIMARY_ALT"] in w.styleSheet()
                or cockpit_theme.LIGHT_TOKENS["TEXT_MUTED"] in w.styleSheet()
            )
            assert cockpit_theme.DARK_TOKENS["TEXT_PRIMARY_ALT"] not in name_lbl.styleSheet()
        finally:
            dlg.deleteLater()

    def test_accounts_retheme_never_rereads_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        from agent_takkub import accounts_adapter

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        try:
            QThreadPool.globalInstance().waitForDone(5_000)
            app = QApplication.instance()
            for _ in range(10):
                app.processEvents()

            def _boom():
                raise AssertionError("retheme must not re-read credentials")

            monkeypatch.setattr(accounts_adapter, "provider_rows", _boom)
            cockpit_theme.apply_variant("light")
            dlg.retheme()  # must not raise
        finally:
            dlg.deleteLater()

    def test_usage_card_label_repaints_on_live_theme_switch(self) -> None:
        from agent_takkub import usage_ledger

        usage_ledger.record_turn(
            "claude",
            "default",
            "2026-09-05T10:00:00Z",
            "r1",
            "claude-sonnet-5",
            {"input": 10, "cache_creation": 20, "cache_read": 30, "output": 40},
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USAGE)
        try:
            assert dlg._usage_last_result is not None

            def _card_value_label():
                card = dlg._usage_cards_row.itemAt(0).widget()
                if card is None:
                    return None
                labels = card.findChildren(settings_window.QLabel)
                return labels[-1] if labels else None

            before = _card_value_label()
            assert before is not None, "seeded a real turn — a usage card must render"
            assert cockpit_theme.DARK_TOKENS["TEXT_PRIMARY_ALT"] in before.styleSheet()

            cockpit_theme.apply_variant("light")
            dlg.retheme()

            after = _card_value_label()
            assert cockpit_theme.LIGHT_TOKENS["TEXT_PRIMARY_ALT"] in after.styleSheet()
            assert cockpit_theme.DARK_TOKENS["TEXT_PRIMARY_ALT"] not in after.styleSheet()
        finally:
            dlg.deleteLater()

    def test_usage_retheme_never_requeries_the_ledger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import usage_ledger

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USAGE)
        try:

            def _boom(**_kw):
                raise AssertionError("retheme must not re-query usage_ledger")

            monkeypatch.setattr(usage_ledger, "query_usage", _boom)
            cockpit_theme.apply_variant("light")
            dlg.retheme()  # must not raise
        finally:
            dlg.deleteLater()

    def test_retheme_is_a_no_op_when_the_page_was_never_built(self) -> None:
        """Guards the `hasattr` gate itself — a future lazy-build refactor
        (backend#2, in flight) may construct pages on first visit only;
        `retheme()` must not crash reaching for caches that don't exist yet."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_GENERAL)
        try:
            del dlg._accounts_rows_box
            del dlg._usage_cards_row
            cockpit_theme.apply_variant("light")
            dlg.retheme()  # must not raise
        finally:
            dlg.deleteLater()
