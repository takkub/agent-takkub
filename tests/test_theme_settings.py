"""Tests for theme_settings (#506) — persisted theme mode + system light/dark
detection. Companion to test_cockpit_theme.py's TestThemeVariants (token sets)
and test_settings_theme_toggle.py (the Settings UI wiring)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import theme_settings


class TestPersistence:
    def test_load_defaults_dark_when_missing(self, tmp_path: Path) -> None:
        assert theme_settings.load(tmp_path / "nope.json") == "dark"

    def test_save_load_round_trip_all_modes(self, tmp_path: Path) -> None:
        target = tmp_path / "theme-settings.json"
        for mode in theme_settings.MODES:
            assert theme_settings.save(mode, target) is True
            assert theme_settings.load(target) == mode

    def test_invalid_payload_falls_back_to_default(self, tmp_path: Path) -> None:
        target = tmp_path / "theme-settings.json"
        for content in ("not json", '{"mode": "neon"}', '["mode"]', '{"mode": 3}'):
            target.write_text(content, encoding="utf-8")
            assert theme_settings.load(target) == theme_settings.DEFAULT_MODE

    def test_save_rejects_unknown_mode(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            theme_settings.save("neon", tmp_path / "theme-settings.json")

    def test_default_path_lives_under_settings_home(self) -> None:
        from agent_takkub import config

        assert theme_settings.path().parent == config.SETTINGS_HOME


class TestResolveVariant:
    def test_explicit_modes_pass_through(self) -> None:
        assert theme_settings.resolve_variant("light") == "light"
        assert theme_settings.resolve_variant("dark") == "dark"

    def test_system_mode_uses_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(theme_settings, "detect_system_variant", lambda: "light")
        assert theme_settings.resolve_variant("system") == "light"
        monkeypatch.setattr(theme_settings, "detect_system_variant", lambda: "dark")
        assert theme_settings.resolve_variant("system") == "dark"


class TestDetectSystemVariant:
    """Both platform branches must exist and be exercised — the cross-platform
    rule (CLAUDE.md) forbids a sys.platform gate with a missing other side."""

    def test_windows_branch_reads_apps_use_light_theme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_settings.sys, "platform", "win32")
        import sys as _sys
        import types

        queried: dict[str, str] = {}

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        for reg_value, expected in ((1, "light"), (0, "dark")):
            fake_winreg = types.SimpleNamespace(
                HKEY_CURRENT_USER=object(),
                OpenKey=lambda root, subkey: queried.update(subkey=subkey) or _Key(),
                QueryValueEx=lambda key, name, v=reg_value: queried.update(name=name) or (v, 4),
            )
            monkeypatch.setitem(_sys.modules, "winreg", fake_winreg)
            assert theme_settings.detect_system_variant() == expected
        assert queried["name"] == "AppsUseLightTheme"
        assert queried["subkey"].endswith("Personalize")

    def test_windows_branch_registry_failure_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_settings.sys, "platform", "win32")
        import types

        def boom(*a, **k):
            raise OSError("no registry")

        monkeypatch.setitem(
            __import__("sys").modules,
            "winreg",
            types.SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=boom),
        )
        assert theme_settings.detect_system_variant() in theme_settings.VARIANTS

    def test_darwin_branch_maps_interface_style(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(theme_settings.sys, "platform", "darwin")

        class _Proc:
            def __init__(self, code: int, out: str) -> None:
                self.returncode = code
                self.stdout = out

        # Dark mode: `defaults read -g AppleInterfaceStyle` exits 0 with "Dark".
        monkeypatch.setattr(theme_settings.subprocess, "run", lambda *a, **k: _Proc(0, "Dark\n"))
        assert theme_settings.detect_system_variant() == "dark"
        # Light mode: the key does not exist → non-zero exit IS the answer.
        monkeypatch.setattr(theme_settings.subprocess, "run", lambda *a, **k: _Proc(1, ""))
        assert theme_settings.detect_system_variant() == "light"

    def test_darwin_branch_subprocess_failure_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_settings.sys, "platform", "darwin")

        def boom(*a, **k):
            raise OSError("no defaults binary")

        monkeypatch.setattr(theme_settings.subprocess, "run", boom)
        assert theme_settings.detect_system_variant() in theme_settings.VARIANTS

    def test_other_platform_falls_back_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_settings.sys, "platform", "linux")
        assert theme_settings.detect_system_variant() in theme_settings.VARIANTS
