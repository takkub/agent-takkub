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
        # Codex Low #10 — the fallback is resolved per-platform among a
        # candidate list (a single hardcoded Windows name would be wrong on
        # macOS/Linux), so assert membership rather than exact equality to
        # one hardcoded name.
        assert result["sans"] in cockpit_theme.FONT_SANS_FALLBACK_CANDIDATES
        assert result["mono"] in cockpit_theme.FONT_MONO_FALLBACK_CANDIDATES

    def test_missing_fonts_dir_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Gemini #18 — a fallback-from-bundled event must not be silent."""
        monkeypatch.setattr(cockpit_theme, "_FONTS_DIR", tmp_path / "nope")
        with caplog.at_level("WARNING", logger="agent_takkub.cockpit_theme"):
            cockpit_theme.ensure_fonts_loaded()
        assert any("bundled" in rec.message for rec in caplog.records)

    def test_resolve_fallback_family_prefers_first_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cockpit_theme.QFontDatabase, "families", staticmethod(lambda: ["Zzz", "Arial"])
        )
        assert cockpit_theme._resolve_fallback_family(("Segoe UI", "Arial")) == "Arial"

    def test_resolve_fallback_family_defaults_when_none_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cockpit_theme.QFontDatabase, "families", staticmethod(lambda: []))
        assert cockpit_theme._resolve_fallback_family(("Segoe UI", "Arial")) == "Segoe UI"

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

    def test_dark_qss_styles_combobox_spinbox_and_scrollbars(self) -> None:
        """Codex Low #9 — these subcontrols previously fell back to native
        rendering, breaking the dark theme on some platform styles."""
        qss = cockpit_theme.build_stylesheet("Sans", "Mono")
        for selector in (
            "QComboBox::drop-down",
            "QSpinBox::up-button",
            "QSpinBox:focus",
            "QScrollBar:vertical",
            "QScrollBar::handle:vertical",
        ):
            assert selector in qss

    def test_nav_indicator_selector_present(self) -> None:
        """Gemini #13 — the active-nav 5px rounded bar is a real QFrame, not
        a border-left (QSS can't round only one side of a border)."""
        qss = cockpit_theme.build_stylesheet("Sans", "Mono")
        assert "#navIndicator" in qss

    def test_spinbox_arrows_use_svg_image_not_border_triangle(self) -> None:
        """Critic #2026-07-10 v2 (bug #3 loop 2) — Qt stylesheets do not
        render ::up-arrow/::down-arrow from border-* (that's a CSS-only
        trick); Qt only draws sub-control arrows from image:/border-image:.
        The old border-triangle QSS left the New Role QSpinBox arrows
        invisible on a real display even though tofu-only tests passed."""
        qss = cockpit_theme.build_stylesheet("Sans", "Mono")
        assert "QSpinBox::up-arrow" in qss
        assert "QSpinBox::down-arrow" in qss
        assert "image: url(" in qss
        # the old broken approaches must be gone, not just additively present
        assert "border-bottom: 5px solid" not in qss
        assert "border-top: 5px solid" not in qss

    def test_spinbox_arrow_uses_real_svg_file_not_data_uri(self) -> None:
        """Critic #2026-07-10 v3 (bug #3 loop 3) — pixel-measured proof that
        Qt QSS url(data:image/svg+xml;...) does NOT render (0 bright pixels
        in the button strip on a real display, both utf8 and base64
        encodings); only url() pointing at a real file on disk rendered the
        glyph (evidence-spinbox-filefix-works.png). The data-URI approach
        must be fully gone, replaced by a real file path."""
        qss = cockpit_theme.build_stylesheet("Sans", "Mono")
        assert "data:image/svg+xml" not in qss
        assert "spin-up.svg" in qss
        assert "spin-down.svg" in qss
        # as_posix() must be used — a raw Windows path would leak backslashes
        # into the QSS url(), which Qt's stylesheet parser does not accept.
        assert "\\" not in qss

    def test_spinbox_arrow_svg_files_exist_on_disk(self) -> None:
        """The QSS references image: url("<path>") — the referenced files
        must actually exist on disk at build_stylesheet() time, not just be
        a plausible-looking path string."""
        icons_dir = Path(cockpit_theme.__file__).parent / "static" / "icons"
        for name in (
            "spin-up.svg",
            "spin-down.svg",
            "spin-up-disabled.svg",
            "spin-down-disabled.svg",
        ):
            assert (icons_dir / name).exists()


class TestGoldButtonGlow:
    def test_gold_button_has_drop_shadow_effect(self) -> None:
        """Gemini #11 — QSS can't render a drop-shadow; a real
        QGraphicsDropShadowEffect must back every gold CTA (Save & Apply,
        Create Role — both built via this same factory)."""
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect

        btn = cockpit_theme.gold_button("Save & Apply")
        effect = btn.graphicsEffect()
        assert isinstance(effect, QGraphicsDropShadowEffect)
        assert effect.blurRadius() == 18


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

    def test_disabled_toggle_still_reports_enabled_state(self) -> None:
        """Codex Low #8 / Gemini #15 — paintEvent now branches on
        isEnabled(); this doesn't visually assert pixels, but pins the
        contract the paint logic reads (a disabled+checked switch, e.g. the
        locked Lead row, must not be indistinguishable from a live one)."""
        sw = cockpit_theme.ToggleSwitch(checked=True)
        sw.setEnabled(False)
        assert sw.isEnabled() is False
        assert sw.isChecked() is True
        sw.repaint()  # exercises paintEvent's disabled branch without raising


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

    def test_gold_soft_chip_compact_uses_smaller_padding_and_font(self) -> None:
        """Critic #2026-07-10 v2 — the BUILT-IN chip in a narrow Templates
        list row was crowding out the template name; compact=True must
        shrink its footprint, not just be a no-op alias."""
        default_chip = cockpit_theme.gold_soft_chip("BUILT-IN")
        compact_chip = cockpit_theme.gold_soft_chip("BUILT-IN", compact=True)
        assert "10px" in compact_chip.styleSheet()
        assert compact_chip.styleSheet() != default_chip.styleSheet()
