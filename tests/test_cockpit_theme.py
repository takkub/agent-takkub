"""Tests for cockpit_theme — design tokens + font loading + QSS/widget
helpers backing the new Settings window (2026-07-10 gold/IBM-Plex design)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import cockpit_theme


@pytest.fixture(autouse=True)
def _reset_font_cache(monkeypatch: pytest.MonkeyPatch):
    """`ensure_fonts_loaded()` memoizes into a module global — reset it per
    test so a `_FONTS_DIR` monkeypatch actually takes effect."""
    monkeypatch.setattr(cockpit_theme, "_font_cache", None)
    yield
    monkeypatch.setattr(cockpit_theme, "_font_cache", None)


class TestEnsureFontsLoaded:
    def test_bundled_fonts_load_from_repo(self) -> None:
        # static/fonts/*.ttf ship in the repo (and package-data) — this is
        # the real, non-fallback path.
        result = cockpit_theme.ensure_fonts_loaded()
        assert result["bundled"] is True
        assert result["sans"]
        assert result["mono"]
        assert result["sans"] != cockpit_theme.FONT_SANS_FALLBACK
        assert result["mono"] != cockpit_theme.FONT_MONO_FALLBACK

    def test_missing_fonts_dir_falls_back_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cockpit_theme, "_FONTS_DIR", tmp_path / "nope")
        result = cockpit_theme.ensure_fonts_loaded()
        assert result["bundled"] is False
        assert result["sans"] == cockpit_theme.FONT_SANS_FALLBACK
        assert result["mono"] == cockpit_theme.FONT_MONO_FALLBACK

    def test_result_is_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = cockpit_theme.ensure_fonts_loaded()
        # Changing _FONTS_DIR after the first call must NOT affect the cached
        # result — proves the memoization actually short-circuits.
        monkeypatch.setattr(cockpit_theme, "_FONTS_DIR", Path("/does/not/exist"))
        second = cockpit_theme.ensure_fonts_loaded()
        assert first is second


class TestBuildStylesheet:
    def test_contains_gold_accent_and_font_families(self) -> None:
        qss = cockpit_theme.build_stylesheet("TestSans", "TestMono")
        assert cockpit_theme.ACCENT_GOLD in qss
        assert "TestSans" in qss
        assert "TestMono" in qss

    def test_object_name_selectors_present_for_chrome(self) -> None:
        qss = cockpit_theme.build_stylesheet("Sans", "Mono")
        for selector in (
            "#settingsWindow",
            "#titlebar",
            "#statusStrip",
            "#sidebar",
            "#navButton",
            "#goldButton",
            "#footer",
        ):
            assert selector in qss


class TestToggleSwitch:
    def test_default_unchecked(self) -> None:
        sw = cockpit_theme.ToggleSwitch()
        assert sw.isCheckable() is True
        assert sw.isChecked() is False

    def test_checked_constructor_arg(self) -> None:
        sw = cockpit_theme.ToggleSwitch(checked=True)
        assert sw.isChecked() is True

    def test_toggle_flips_state(self) -> None:
        sw = cockpit_theme.ToggleSwitch(checked=False)
        sw.toggle()
        assert sw.isChecked() is True


class TestWidgetHelpers:
    def test_gold_button_object_name_and_text(self) -> None:
        btn = cockpit_theme.gold_button("Save & Apply")
        assert btn.objectName() == "goldButton"
        assert btn.text() == "Save & Apply"

    def test_secondary_button_object_name(self) -> None:
        btn = cockpit_theme.secondary_button("Cancel")
        assert btn.objectName() == "secondaryButton"

    def test_role_chip_has_dot_and_label(self) -> None:
        chip = cockpit_theme.role_chip("Backend", "#4E86F7")
        assert chip.layout().count() == 2

    def test_gold_soft_chip_shows_text(self) -> None:
        chip = cockpit_theme.gold_soft_chip("feature")
        assert chip.text() == "feature"
