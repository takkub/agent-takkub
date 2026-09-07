"""Persisted cockpit theme mode (#506) + system light/dark detection.

Mirrors :mod:`performance_settings`'s shape: durable JSON under
``config.SETTINGS_HOME``, no Qt dependency at import time (the Qt
``colorScheme`` probe is a lazy in-function fallback only), so settings UI,
app boot, and tests all share one schema.

The *mode* is what the user picked (``system``/``light``/``dark``); the
*variant* is what actually renders (``light``/``dark``) after resolving
``system`` against the OS setting. ``cockpit_theme.apply_variant()`` consumes
the variant — that module stays a pure-Qt leaf and never imports this one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import config
from ._win_console import SUBPROCESS_NO_WINDOW

MODES = ("system", "light", "dark")
# Existing installs have only ever seen the dark cockpit — light is opt-in,
# so an update must not surprise-flip anyone's UI. "system" is one click away.
DEFAULT_MODE = "dark"

VARIANTS = ("light", "dark")


def path() -> Path:
    return config.SETTINGS_HOME / "theme-settings.json"


def load(settings_path: Path | None = None) -> str:
    """Return the persisted theme mode, or :data:`DEFAULT_MODE` when the file
    is missing/invalid — never raises."""
    target = settings_path or path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        mode = str(payload.get("mode", "")).strip().lower()
        return mode if mode in MODES else DEFAULT_MODE
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return DEFAULT_MODE


def save(mode: str, settings_path: Path | None = None) -> bool:
    if mode not in MODES:
        raise ValueError(f"unknown theme mode: {mode!r}")
    target = settings_path or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, {"schema_version": 1, "mode": mode})


def _qt_color_scheme_variant() -> str | None:
    """Qt's own colorScheme hint, when a QGuiApplication is already alive —
    the cross-platform fallback for OSes without a native probe below."""
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:
            return None
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return "light"
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
    except Exception:
        return None
    return None


def detect_system_variant() -> str:
    """The OS-level app theme: ``"light"`` or ``"dark"`` (dark when unknowable).

    Windows reads the registry (``AppsUseLightTheme``); macOS asks
    ``defaults`` (``AppleInterfaceStyle`` exists only in dark mode); everything
    else falls back to Qt's colorScheme hint. Both platform branches are
    explicit — neither OS is left to the fallback by omission.
    """
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) == 1 else "dark"
        except OSError:
            return _qt_color_scheme_variant() or "dark"
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
            # Key present ("Dark") only when dark mode is on; a non-zero exit
            # (key absent) IS the light-mode answer, not an error.
            return "dark" if proc.returncode == 0 and "dark" in proc.stdout.lower() else "light"
        except (OSError, subprocess.SubprocessError):
            return _qt_color_scheme_variant() or "dark"
    return _qt_color_scheme_variant() or "dark"


def resolve_variant(mode: str) -> str:
    """Collapse a persisted *mode* to the concrete render variant."""
    if mode == "light":
        return "light"
    if mode == "dark":
        return "dark"
    return detect_system_variant()
