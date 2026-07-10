"""Design tokens + QSS/widget helpers for the Takkub Cockpit Settings window.

Source of truth: `docs/design-review/2026-07-10-cockpit-settings-design-system.md`
(extracted from the user's canonical `Takkub Cockpit.dc.html` design) — gold
`#E3B341` + IBM Plex, NOT the older teal/indigo palette used elsewhere in the
cockpit. This module is scoped to `settings_window.py` only; it does not
reskin any other dialog.

Pure-Qt leaf module: no `agent_takkub` imports beyond stdlib/PyQt6, so it is
safe for any UI module to depend on without import-linter risk.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFontDatabase, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

_log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Grounds
# ──────────────────────────────────────────────────────────────
GROUND_BODY = "#050608"
GROUND_WINDOW = "#15171c"
GROUND_TITLEBAR = "#0f1114"
STATUS_STRIP_GRAD_TOP = "#181b21"
STATUS_STRIP_GRAD_BOTTOM = "#141519"
GROUND_SIDEBAR = "#101216"
GROUND_PANEL = "#181b21"
GROUND_PANEL_ALT = "#191c22"
GROUND_INPUT = "#1c1f26"
GROUND_SELECT = "#232732"

# ──────────────────────────────────────────────────────────────
# Borders
# ──────────────────────────────────────────────────────────────
BORDER_HAIRLINE = "rgba(255,255,255,0.06)"
BORDER_MED = "rgba(255,255,255,0.09)"
BORDER_STRONG = "rgba(255,255,255,0.12)"
BORDER_STRONG2 = "rgba(255,255,255,0.14)"
RADIUS_SM = 8
RADIUS_MD = 10
RADIUS_LG = 14

# ──────────────────────────────────────────────────────────────
# Accent gold
# ──────────────────────────────────────────────────────────────
ACCENT_GOLD = "#E3B341"
GOLD_GRAD_TOP = "#EEC25A"
GOLD_GRAD_BOTTOM = "#E3B341"
GOLD_TEXT_ON = "#241a00"
GOLD_CHIP_BG = "rgba(227,179,65,0.12)"
GOLD_CHIP_BORDER = "rgba(227,179,65,0.35)"
GOLD_CHIP_TEXT = "#ECCB6A"

# ──────────────────────────────────────────────────────────────
# Text
# ──────────────────────────────────────────────────────────────
TEXT_PRIMARY = "#f2f3f5"
TEXT_PRIMARY_ALT = "#e9ebef"
TEXT_SECONDARY = "#c7ccd4"
TEXT_SECONDARY_ALT = "#cfd3da"
TEXT_MUTED = "#7b828f"
TEXT_MUTED_ALT = "#828a95"
TEXT_FAINT = "#5b626e"
TEXT_FAINT_ALT = "#6b7280"

# ──────────────────────────────────────────────────────────────
# Misc badges
# ──────────────────────────────────────────────────────────────
SUBSTITUTE_BADGE_TEXT = "#E9A876"
SUBSTITUTE_BADGE_BORDER = "rgba(217,119,87,0.4)"
PARALLEL_CHIP_BG = "rgba(164,114,240,.14)"
PARALLEL_CHIP_BORDER = "rgba(164,114,240,.3)"
PARALLEL_CHIP_TEXT = "#c39cf5"

# Role colors per the design doc — a superset/override of roles.py's own
# Role.color (which serves the main grid, a different surface). Custom roles
# or any name not listed here fall back to their own Role.color at the
# call site, not to a value from this dict.
ROLE_COLORS: dict[str, str] = {
    "lead": "#E3B341",
    "frontend": "#34B7AC",
    "backend": "#4E86F7",
    "mobile": "#A472F0",
    "devops": "#43B562",
    "qa": "#E39A3C",
    "reviewer": "#F26D6D",
    "critic": "#F0619A",
    "designer": "#C77DF0",
    "analyst": "#45C4D6",
    "security": "#E0574F",
    "docs": "#8FA3B8",
}

# ──────────────────────────────────────────────────────────────
# Fonts — bundled IBM Plex (OFL, github.com/IBM/plex) with a graceful
# cross-platform fallback. Never blocks boot: a missing/corrupt ttf just
# means the fallback family is used and `ensure_fonts_loaded()["bundled"]`
# comes back False so the caller can flag it.
# ──────────────────────────────────────────────────────────────
FONT_SANS_FALLBACK_CANDIDATES: tuple[str, ...] = (
    "Segoe UI",  # Windows
    "Helvetica Neue",  # macOS
    "Noto Sans",  # common Linux distro default
    "DejaVu Sans",  # near-universal Linux fallback
    "Arial",
)
FONT_MONO_FALLBACK_CANDIDATES: tuple[str, ...] = (
    "Cascadia Mono",  # Windows Terminal / modern Windows
    "SF Mono",  # macOS
    "Menlo",  # older macOS
    "DejaVu Sans Mono",  # near-universal Linux fallback
    "Consolas",
)
# First candidate of each list — kept as plain constants (not just the
# lists) since callers/tests reference "the" fallback name. The family Qt
# actually renders with is resolved per-platform inside
# `ensure_fonts_loaded()` via `_resolve_fallback_family()`, which is what
# matters at runtime; these two stay as the historical single-name default.
FONT_SANS_FALLBACK = FONT_SANS_FALLBACK_CANDIDATES[0]
FONT_MONO_FALLBACK = FONT_MONO_FALLBACK_CANDIDATES[0]

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_FONTS_DIR = _STATIC_DIR / "fonts"

_SANS_FILES = (
    "IBMPlexSans-Regular.ttf",
    "IBMPlexSans-Medium.ttf",
    "IBMPlexSans-SemiBold.ttf",
    "IBMPlexSans-Bold.ttf",
)
_MONO_FILES = (
    "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf",
    "IBMPlexMono-SemiBold.ttf",
)

_font_cache: dict[str, object] | None = None


def _resolve_fallback_family(candidates: tuple[str, ...]) -> str:
    """Pick the first *installed* name from `candidates` (a hardcoded single
    fallback name like "Segoe UI" is only real on Windows — on macOS/Linux
    Qt would silently substitute something uncontrolled). Falls back to the
    first candidate if the font database can't be queried yet (e.g. no
    QApplication) or genuinely none of the candidates are installed — never
    raises."""
    try:
        available = set(QFontDatabase.families())
    except Exception:
        return candidates[0]
    for name in candidates:
        if name in available:
            return name
    return candidates[0]


def ensure_fonts_loaded() -> dict[str, object]:
    """Register the bundled IBM Plex ttfs with Qt (idempotent, cached).

    Returns ``{"sans": family, "mono": family, "bundled": bool}``. ``sans``/
    ``mono`` are the actual family names Qt registered the fonts under (not
    necessarily "IBM Plex Sans" verbatim — Qt reads it from the font's own
    name table) when at least one weight loaded, otherwise the platform
    fallback family. ``bundled`` is True only when BOTH families loaded from
    disk — a partial load (e.g. Sans ok, Mono missing) still reports the
    fallback for the missing family without raising.
    """
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    sans_family: str | None = None
    for fname in _SANS_FILES:
        path = _FONTS_DIR / fname
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families and sans_family is None:
            sans_family = families[0]

    mono_family: str | None = None
    for fname in _MONO_FILES:
        path = _FONTS_DIR / fname
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families and mono_family is None:
            mono_family = families[0]

    bundled = sans_family is not None and mono_family is not None
    if not bundled:
        _log.warning(
            "ensure_fonts_loaded: bundled IBM Plex fonts missing/failed to load from %s "
            "(sans=%s, mono=%s) — falling back to platform font substitution",
            _FONTS_DIR,
            "ok" if sans_family else "missing",
            "ok" if mono_family else "missing",
        )
    _font_cache = {
        "sans": sans_family or _resolve_fallback_family(FONT_SANS_FALLBACK_CANDIDATES),
        "mono": mono_family or _resolve_fallback_family(FONT_MONO_FALLBACK_CANDIDATES),
        "bundled": bundled,
    }
    return _font_cache


# ──────────────────────────────────────────────────────────────
# QSS
# ──────────────────────────────────────────────────────────────


def build_stylesheet(sans_family: str, mono_family: str) -> str:
    """Return the full QSS for the Settings window, parameterized by the
    resolved font families (bundled IBM Plex or the platform fallback)."""
    return f"""
    QDialog#settingsWindow {{
        background: {GROUND_WINDOW};
        color: {TEXT_PRIMARY};
        font-family: "{sans_family}";
        font-size: 13px;
    }}
    QWidget#titlebar {{
        background: {GROUND_TITLEBAR};
        border-bottom: 1px solid {BORDER_HAIRLINE};
    }}
    QLabel#titlebarLabel {{
        font-family: "{mono_family}";
        color: {TEXT_SECONDARY};
        font-size: 12px;
    }}
    QLabel#titlebarDots {{
        color: {TEXT_FAINT};
        letter-spacing: 3px;
    }}
    QWidget#statusStrip {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 {STATUS_STRIP_GRAD_TOP}, stop:1 {STATUS_STRIP_GRAD_BOTTOM});
        border-bottom: 1px solid {BORDER_HAIRLINE};
    }}
    QLabel#statusBrand {{
        font-family: "{mono_family}";
        font-weight: 600;
        color: {TEXT_SECONDARY};
        font-size: 11px;
        letter-spacing: 1px;
    }}
    QLabel#statusVersion {{
        font-family: "{mono_family}";
        color: {TEXT_FAINT};
        font-size: 11px;
    }}
    QWidget#sidebar {{
        background: {GROUND_SIDEBAR};
        border-right: 1px solid {BORDER_HAIRLINE};
    }}
    QLabel#sidebarSection {{
        font-family: "{mono_family}";
        color: {TEXT_FAINT};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1.5px;
        padding: 12px 14px 4px 14px;
    }}
    QPushButton#navButton {{
        text-align: left;
        padding: 8px 12px 8px 10px;
        border: none;
        background: transparent;
        color: {TEXT_SECONDARY};
        font-size: 13px;
        border-radius: 0px;
    }}
    QPushButton#navButton:hover {{
        background: rgba(255,255,255,0.04);
        color: {TEXT_PRIMARY};
    }}
    QPushButton#navButton[active="true"] {{
        background: {GOLD_CHIP_BG};
        color: {TEXT_PRIMARY};
        font-weight: 600;
    }}
    QFrame#navIndicator {{
        background: {ACCENT_GOLD};
        border-radius: 2px;
    }}
    QPushButton#newRoleButton {{
        margin: 10px 12px 12px 12px;
        padding: 8px 10px;
        border-radius: {RADIUS_SM}px;
        border: 1px solid {GOLD_CHIP_BORDER};
        background: {GOLD_CHIP_BG};
        color: {GOLD_CHIP_TEXT};
        font-weight: 600;
    }}
    QPushButton#newRoleButton:hover {{
        background: rgba(227,179,65,0.18);
    }}
    QWidget#content {{
        background: {GROUND_WINDOW};
    }}
    QLabel#contentTitle {{
        font-size: 20px;
        font-weight: 700;
        color: {TEXT_PRIMARY_ALT};
    }}
    QLabel#contentSub {{
        font-size: 13px;
        color: {TEXT_MUTED};
    }}
    QWidget#footer {{
        background: {GROUND_SIDEBAR};
        border-top: 1px solid {BORDER_HAIRLINE};
    }}
    QLabel#unsavedDot {{
        color: {ACCENT_GOLD};
        font-size: 16px;
    }}
    QLabel#unsavedLabel {{
        color: {TEXT_MUTED};
        font-size: 12px;
    }}
    QPushButton#goldButton {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 {GOLD_GRAD_TOP}, stop:1 {GOLD_GRAD_BOTTOM});
        color: {GOLD_TEXT_ON};
        font-weight: 700;
        border: none;
        border-radius: {RADIUS_SM}px;
        padding: 8px 18px;
    }}
    QPushButton#goldButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 #f2cd75, stop:1 {GOLD_GRAD_TOP});
    }}
    QPushButton#goldButton:disabled {{
        background: {GROUND_SELECT};
        color: {TEXT_FAINT};
    }}
    QPushButton#secondaryButton {{
        background: transparent;
        border: 1px solid {BORDER_STRONG};
        color: {TEXT_SECONDARY};
        border-radius: {RADIUS_SM}px;
        padding: 8px 16px;
    }}
    QPushButton#secondaryButton:hover {{
        background: rgba(255,255,255,0.05);
        color: {TEXT_PRIMARY};
    }}
    QWidget#panel {{
        background: {GROUND_PANEL};
        border: 1px solid {BORDER_HAIRLINE};
        border-radius: {RADIUS_MD}px;
    }}
    QWidget#panelAlt {{
        background: {GROUND_PANEL_ALT};
        border: 1px solid {BORDER_HAIRLINE};
        border-radius: {RADIUS_MD}px;
    }}
    QLabel#panelTitle {{
        font-weight: 600;
        font-size: 13px;
        color: {TEXT_PRIMARY};
    }}
    QLabel#panelHint {{
        color: {TEXT_MUTED};
        font-size: 12px;
    }}
    QLabel#infoBanner {{
        background: {GOLD_CHIP_BG};
        border: 1px solid {GOLD_CHIP_BORDER};
        border-radius: {RADIUS_SM}px;
        color: {TEXT_SECONDARY_ALT};
        padding: 8px 12px;
        font-size: 12px;
    }}
    QLabel#substituteBadge {{
        color: {SUBSTITUTE_BADGE_TEXT};
        border: 1px solid {SUBSTITUTE_BADGE_BORDER};
        border-radius: 999px;
        padding: 1px 8px;
        font-size: 11px;
        font-weight: 600;
    }}
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
        background: {GROUND_INPUT};
        border: 1px solid {BORDER_MED};
        border-radius: {RADIUS_SM}px;
        padding: 6px 8px;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_GOLD};
        selection-color: {GOLD_TEXT_ON};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {ACCENT_GOLD};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        width: 8px;
        height: 8px;
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background: {GROUND_SELECT};
        border: 1px solid {BORDER_STRONG};
        outline: none;
        color: {TEXT_PRIMARY};
        selection-background-color: {ACCENT_GOLD};
        selection-color: {GOLD_TEXT_ON};
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background: {GROUND_SELECT};
        border: none;
        width: 16px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
        background: {BORDER_STRONG};
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_STRONG};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
        border: none;
        background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {BORDER_STRONG};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
        border: none;
        background: none;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
    }}
    QLabel#placeholderBadge {{
        background: rgba(255,255,255,0.05);
        border: 1px dashed {BORDER_STRONG};
        border-radius: {RADIUS_MD}px;
        color: {TEXT_MUTED};
        padding: 24px;
        font-size: 13px;
    }}
    QScrollArea {{
        background: transparent;
        border: none;
    }}
    QScrollArea > QWidget {{
        background: transparent;
    }}
    QScrollArea > QWidget > QWidget {{
        background: transparent;
    }}
    """


# ──────────────────────────────────────────────────────────────
# Reusable widgets
# ──────────────────────────────────────────────────────────────


class ToggleSwitch(QAbstractButton):
    """Rounded track + knob toggle — checked (on) renders gold, matching the
    design system's ``on = gold`` component spec. Implemented as a custom
    ``QAbstractButton`` (per the design doc's own implement note) rather than
    a styled ``QCheckBox`` indicator, since QSS can't draw a sliding knob."""

    def __init__(self, parent: QWidget | None = None, checked: bool = False) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(36, 20)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def paintEvent(self, _event) -> None:
        """Draws checked=gold / unchecked=neutral per the design system, with
        a distinct muted rendering when `isEnabled()` is False (e.g. the
        locked Lead row) — an earlier version ignored enabled state entirely
        and a disabled switch looked identical to a live, clickable one."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        enabled = self.isEnabled()
        checked = self.isChecked()

        if checked:
            track = QColor(ACCENT_GOLD) if enabled else QColor(TEXT_FAINT)
        else:
            track = QColor(GROUND_SELECT)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        knob_d = rect.height() - 4
        knob_x = rect.right() - knob_d - 1 if checked else rect.left() + 1
        if checked:
            knob = QColor(GOLD_TEXT_ON) if enabled else QColor(GROUND_PANEL)
        else:
            knob = QColor(TEXT_MUTED) if enabled else QColor(TEXT_FAINT)
        painter.setBrush(knob)
        painter.drawEllipse(int(knob_x), rect.top() + 2, knob_d, knob_d)

        if self.hasFocus() and enabled:
            pen = QPen(QColor(ACCENT_GOLD))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)


def gold_button(text: str, parent: QWidget | None = None) -> QPushButton:
    """The design system's primary CTA — gradient fill, dark text, bold,
    plus the gold drop-shadow glow the design spec calls for (Gemini #11 —
    QSS alone can't render a drop-shadow, so this attaches a real
    QGraphicsDropShadowEffect: blur 18, offset (0, 6), gold @ 60% opacity)."""
    btn = QPushButton(text, parent)
    btn.setObjectName("goldButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    glow = QGraphicsDropShadowEffect(btn)
    glow.setBlurRadius(18)
    glow.setColor(QColor(227, 179, 65, 153))
    glow.setOffset(0, 6)
    btn.setGraphicsEffect(glow)
    return btn


def secondary_button(text: str, parent: QWidget | None = None) -> QPushButton:
    """Transparent + bordered secondary action button."""
    btn = QPushButton(text, parent)
    btn.setObjectName("secondaryButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def role_chip(label: str, color: str, parent: QWidget | None = None) -> QWidget:
    """Colored dot + label, matching the design system's role-chip component."""
    mono = ensure_fonts_loaded()["mono"]
    chip = QWidget(parent)
    lay = QHBoxLayout(chip)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    dot = QLabel(chip)
    dot.setFixedSize(8, 8)
    dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
    lay.addWidget(dot)
    text = QLabel(label, chip)
    # Gemini #14 — chips/badges spec IBM Plex Mono; left unset here they
    # silently inherited the dialog's sans font instead.
    text.setStyleSheet(f'font-family: "{mono}"; color: {color}; font-weight: 600; font-size: 12px;')
    lay.addWidget(text)
    return chip


def gold_soft_chip(text: str, parent: QWidget | None = None) -> QLabel:
    """The gold "soft chip" — e.g. the active-template badge in the status strip."""
    mono = ensure_fonts_loaded()["mono"]
    chip = QLabel(text, parent)
    chip.setStyleSheet(
        f'font-family: "{mono}"; background: {GOLD_CHIP_BG}; border: 1px solid {GOLD_CHIP_BORDER};'
        f" border-radius: 999px; color: {GOLD_CHIP_TEXT}; padding: 2px 10px;"
        f" font-size: 11px; font-weight: 600;"
    )
    return chip
