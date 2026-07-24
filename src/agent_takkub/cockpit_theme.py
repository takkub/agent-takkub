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
    QMessageBox,
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
# ToggleSwitch's unchecked track — deliberately lighter than GROUND_SELECT.
# GROUND_SELECT against card background GROUND_PANEL (#181b21) had almost no
# delta, so an off toggle's rounded-rect shape barely read against the card
# behind it (design review 2026-07-24 #4, gemini + critic both flagged it).
TOGGLE_TRACK_OFF = "#2d323e"

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

# Neutral (non-gold) chip — read-only ownership badges (BUILT-IN / MANAGED /
# EXTERNAL) in the settings-management redesign (#103 follow-up). Deliberately
# NOT gold: gold means "you can edit/delete this", these mean the opposite.
NEUTRAL_CHIP_BG = "rgba(255,255,255,0.06)"
NEUTRAL_CHIP_BORDER = BORDER_STRONG
NEUTRAL_CHIP_TEXT = TEXT_SECONDARY

# Semantic error/blocked chip — e.g. "BLOCKED BY COCKPIT" on a denylisted
# plugin (settings-management Plugins page, #103 follow-up). SPEC.md "status
# ไม่สื่อด้วยสีอย่างเดียว ใช้ text badge คู่กัน" — pair with explicit text, never
# color alone. Deliberately its own hue (not gold, not neutral): this state
# means "cockpit refuses this", distinct from both "editable" and "read-only".
ERROR_CHIP_BG = "rgba(217,90,90,0.12)"
ERROR_CHIP_BORDER = "rgba(217,90,90,0.35)"
ERROR_CHIP_TEXT = "#e58080"

# ──────────────────────────────────────────────────────────────
# Provider brand colors (codex/gemini panes) — identity, NOT "active/
# primary". Kept distinct from the gold accent. Mirrored by roles.py
# Role.color for the matching roles; equality is guarded by
# tests/test_role_registry_sync.py so the two never drift.
# ──────────────────────────────────────────────────────────────
PROVIDER_CODEX = "#10a37f"  # OpenAI teal
PROVIDER_GEMINI = "#4285f4"  # Google blue
PROVIDER_OPENCODE = "#f97316"  # sst orange

# ──────────────────────────────────────────────────────────────
# State colors — status semantics (ok/warn/error/info). The *meaning* is
# intentional and survives migration: never turn these gold, only tokenize
# the value. `_BRIGHT` variants are for small dots/glyphs on dark grounds
# where the base ramp reads too dim (task_dock / agent_pane status dots).
# ──────────────────────────────────────────────────────────────
STATE_OK = "#43B562"
STATE_WARN = "#d97706"
STATE_ERROR = "#ef4444"
STATE_INFO = "#4E86F7"
STATE_OK_BRIGHT = "#22c55e"
STATE_WARN_BRIGHT = "#facc15"
STATE_ERROR_BRIGHT = "#f87171"
STATE_INFO_BRIGHT = "#0ea5e9"
STATE_EXITED = "#f97316"  # orange — a pane exited unexpectedly (respawnable)
# Amber used for "pro/enabled/attention" chips (status_header, main_window) —
# a brighter amber than STATE_WARN's provider-warn tone; kept distinct so both
# survive migration at their exact values.
STATE_WARN_ALT = "#f59e0b"

# ──────────────────────────────────────────────────────────────
# Status-bar chip identity accents (status_header toggles). Each toggle has
# its own meaning-carrying "on" color, distinct from the gold primary accent;
# tokenized (not gold) so the identities survive migration. The "off" state of
# all of them is the neutral TEXT_MUTED.
# ──────────────────────────────────────────────────────────────
CHIP_PLAN_MAX = "#8b5cf6"  # violet — Max plan (1M context)
CHIP_EXEC_PARALLEL = "#10b981"  # emerald — PARALLEL execution mode active
CHIP_REMOTE_ON = "#14b8a6"  # teal — Remote server live

# Neutral slate fallback for a role with no ROLE_COLORS/Role.color entry
# (e.g. an unknown/legacy role name at a chip call site). Same hue as the
# shell role.
ROLE_COLOR_FALLBACK = "#94a3b8"

# ──────────────────────────────────────────────────────────────
# Anthropic clay — the token/usage-meter accent. A real 4th brand color,
# distinct from gold; meters/usage surfaces only.
# ──────────────────────────────────────────────────────────────
METER_CLAY = "#d97757"
METER_CLAY_ALT = "#e08968"
# Meter/usage state ramp amber (token_meter/usage_meter/limit_panel + the rtk
# install nudge) — the mid "getting full / attention" fill.
METER_AMBER = "#fbbf24"
METER_AMBER_LIGHT = "#fcd34d"

# ──────────────────────────────────────────────────────────────
# Banner state triples (bg / border / text) for inline notice banners
# (update_panel). Meaning-preserving tokenization of the old literals.
# ──────────────────────────────────────────────────────────────
BANNER_WARN_BG = "#422006"
BANNER_WARN_BORDER = "#a16207"
BANNER_WARN_TEXT = "#fde047"
BANNER_WARN_HOVER = "#713f12"
BANNER_OK_BG = "#052e16"
BANNER_OK_BORDER = "#166534"
BANNER_OK_TEXT = "#4ade80"
BANNER_OK_HOVER = "#14532d"
BANNER_ERROR_BG = "#450a0a"
BANNER_ERROR_BORDER = "#7f1d1d"
BANNER_ERROR_TEXT = "#fca5a5"
# INFO banner is used as a light-filled button in update_panel (dark text on a
# light-blue fill), so the "text"/"bg" here read inverted vs the dark warn/ok/
# error banners — the values are what matters and are shared by both uses.
BANNER_INFO_BG = "#1e3a8a"
BANNER_INFO_BORDER = "#2563eb"
BANNER_INFO_TEXT = "#93c5fd"
BANNER_INFO_HOVER = "#bfdbfe"

# Deterministic per-project avatar tints (hash → palette) — a distinct
# purpose from ROLE_COLORS (role identity), intentionally its own 10-color
# spread so adjacent projects read apart. Canonical home for what
# project_nav historically defined inline as `_AVATAR_COLORS` (values kept
# verbatim so existing project avatars don't change hue).
AVATAR_TINTS: tuple[str, ...] = (
    "#6366f1",
    "#8b5cf6",
    "#ec4899",
    "#f43f5e",
    "#f59e0b",
    "#10b981",
    "#06b6d4",
    "#3b82f6",
    "#a855f7",
    "#14b8a6",
)

# Role colors — the SINGLE source of truth for role identity across every
# cockpit surface (grid + Settings). roles.py Role.color mirrors these exact
# values for its built-in roles (guarded by tests/test_role_registry_sync.py);
# call sites read `ROLE_COLORS.get(name, role.color)` so a custom role not in
# this dict falls back to its own Role.color. codex/gemini reuse the
# PROVIDER_* brand tokens; shell is a neutral slate.
ROLE_COLORS: dict[str, str] = {
    "lead": "#E3B341",
    "frontend": "#34B7AC",
    "backend": "#4E86F7",
    "mobile": "#A472F0",
    "devops": "#43B562",
    "gemini": PROVIDER_GEMINI,
    "qa": "#E39A3C",
    "reviewer": "#F26D6D",
    "codex": PROVIDER_CODEX,
    "critic": "#F0619A",
    "shell": "#94a3b8",
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
# Neither IBM Plex Sans/Mono nor the sans fallback candidates above carry Thai
# glyphs — declaring only `sans_family` in the QSS font-family (no fallback
# chain at all) left every Thai string in the window rendering as tofu (design
# review 2026-07-24, root cause #2). `Noto Sans Thai` (OFL, bundled below,
# same pattern as IBM Plex) covers this on any OS; the OS-native names are a
# second-line fallback in case the bundled ttf ever fails to load.
FONT_THAI_FALLBACK_CANDIDATES: tuple[str, ...] = (
    "Leelawadee UI",  # Windows
    "Thonburi",  # macOS
    "Noto Sans Thai",  # common Linux distro default
    "Tahoma",  # older Windows fallback with Thai coverage
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
_THAI_FILES = ("NotoSansThai-Regular.ttf",)

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


def _load_font_family(files: tuple[str, ...]) -> str | None:
    """Register the first weight of *files* that exists+loads and return the
    family Qt registered it under (``None`` if none loaded)."""
    family: str | None = None
    for fname in files:
        path = _FONTS_DIR / fname
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families and family is None:
            family = families[0]
    return family


def ensure_fonts_loaded() -> dict[str, object]:
    """Register the bundled IBM Plex + Noto Sans Thai ttfs with Qt (idempotent,
    cached).

    Returns ``{"sans": family, "mono": family, "thai": family, "bundled":
    bool}``. ``sans``/``mono``/``thai`` are the actual family names Qt
    registered the fonts under (not necessarily "IBM Plex Sans" verbatim — Qt
    reads it from the font's own name table) when at least one weight loaded,
    otherwise the platform fallback family. ``bundled`` is True only when BOTH
    ``sans`` and ``mono`` loaded from disk — a partial load (e.g. Sans ok,
    Mono missing) still reports the fallback for the missing family without
    raising. ``thai`` is tracked separately (own fallback candidates) since it
    backs a font-family *stack*, not a standalone declaration — see
    ``build_stylesheet``'s ``_sans_font_stack``.
    """
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    sans_family = _load_font_family(_SANS_FILES)
    mono_family = _load_font_family(_MONO_FILES)
    thai_family = _load_font_family(_THAI_FILES)

    bundled = sans_family is not None and mono_family is not None
    if not bundled:
        _log.warning(
            "ensure_fonts_loaded: bundled IBM Plex fonts missing/failed to load from %s "
            "(sans=%s, mono=%s) — falling back to platform font substitution",
            _FONTS_DIR,
            "ok" if sans_family else "missing",
            "ok" if mono_family else "missing",
        )
    if thai_family is None:
        _log.warning(
            "ensure_fonts_loaded: bundled Noto Sans Thai missing/failed to load from %s "
            "— falling back to platform Thai font substitution",
            _FONTS_DIR,
        )
    _font_cache = {
        "sans": sans_family or _resolve_fallback_family(FONT_SANS_FALLBACK_CANDIDATES),
        "mono": mono_family or _resolve_fallback_family(FONT_MONO_FALLBACK_CANDIDATES),
        "thai": thai_family or _resolve_fallback_family(FONT_THAI_FALLBACK_CANDIDATES),
        "bundled": bundled,
    }
    return _font_cache


# ──────────────────────────────────────────────────────────────
# QSS
# ──────────────────────────────────────────────────────────────


def _sans_font_stack(sans_family: str) -> str:
    """Comma-separated ``font-family`` value: *sans_family* first, then a Thai
    fallback chain. Qt's QSS font-family list does per-*character* fallback —
    declaring only ``sans_family`` (as the old single-name QSS did) meant any
    Thai codepoint IBM Plex Sans lacks a glyph for had nowhere left to fall
    back to and rendered as tofu (design review 2026-07-24, root cause #2).
    The bundled Noto Sans Thai family goes first in the Thai chain since it's
    guaranteed present regardless of OS; the OS-native names are a second-line
    fallback for the rare case that ttf failed to register."""
    thai_family = str(ensure_fonts_loaded().get("thai") or "")
    names = [sans_family]
    for candidate in (thai_family, *FONT_THAI_FALLBACK_CANDIDATES):
        if candidate and candidate not in names:
            names.append(candidate)
    return ", ".join(f'"{n}"' for n in names)


def build_stylesheet(sans_family: str, mono_family: str) -> str:
    """Return the full QSS for the Settings window, parameterized by the
    resolved font families (bundled IBM Plex or the platform fallback)."""
    # Qt QSS's url(data:image/svg+xml;...) does not render (proven by pixel
    # measurement — see docs/design-review/2026-07-10-settings-ui-visual-critic.md
    # round 3): only url() pointing at a real file on disk renders the glyph.
    _icons_dir = Path(__file__).parent / "static" / "icons"
    _up_arrow_svg = (_icons_dir / "spin-up.svg").as_posix()
    _down_arrow_svg = (_icons_dir / "spin-down.svg").as_posix()
    _up_arrow_svg_disabled = (_icons_dir / "spin-up-disabled.svg").as_posix()
    _down_arrow_svg_disabled = (_icons_dir / "spin-down-disabled.svg").as_posix()
    _combo_arrow_on_svg = (_icons_dir / "combo-down-on.svg").as_posix()
    _sans_stack = _sans_font_stack(sans_family)
    return f"""
    QDialog#settingsWindow, QWidget#settingsWindow {{
        background: {GROUND_WINDOW};
        color: {TEXT_PRIMARY};
        font-family: {_sans_stack};
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
    QLabel#contentPreTitle {{
        font-family: "{mono_family}";
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1.5px;
        color: {ACCENT_GOLD};
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
        border-top: 1px solid {BORDER_STRONG2};
    }}
    /* A real painted dot, not the "●" text glyph the old rule sized via
       font-size — that glyph tofus on fonts lacking it (design review
       2026-07-24 #4). min-width/height give it a circle footprint even if
       the widget keeps QLabel with empty text instead of becoming a QFrame. */
    QLabel#unsavedDot, QFrame#unsavedDot {{
        background: {ACCENT_GOLD};
        border-radius: 4px;
        min-width: 8px;
        min-height: 8px;
        max-width: 8px;
        max-height: 8px;
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
    QWidget#providerRow, QWidget#roleRow {{
        background: {GROUND_PANEL_ALT};
        border: 1px solid {BORDER_HAIRLINE};
        border-radius: {RADIUS_SM}px;
    }}
    QLabel#matrixHeaderCell {{
        font-family: "{mono_family}";
        font-weight: 600;
        font-size: 11px;
        color: {TEXT_SECONDARY};
        padding-bottom: 6px;
        border-bottom: 1px solid {BORDER_HAIRLINE};
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
    QLabel#capabilityWarning {{
        color: {SUBSTITUTE_BADGE_TEXT};
        border: 1px solid {SUBSTITUTE_BADGE_BORDER};
        border-radius: 999px;
        padding: 1px 8px;
        font-size: 11px;
        font-weight: 700;
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
        border-left: 1px solid {BORDER_HAIRLINE};
        width: 22px;
    }}
    /* Styling ::down-arrow at all suppresses Qt's native arrow, so an explicit
       glyph is required or the combo renders as a bare text field and nobody
       realizes it drops down. Same SVG-on-disk approach as QSpinBox above
       (url(data:...) doesn't render, border-triangles come out as rectangles
       in Qt6 — both proven by pixel measurement). */
    QComboBox::down-arrow {{
        image: url("{_down_arrow_svg}");
        width: 8px;
        height: 5px;
    }}
    QComboBox::down-arrow:on {{
        image: url("{_combo_arrow_on_svg}");
    }}
    QComboBox::down-arrow:disabled {{
        image: url("{_down_arrow_svg_disabled}");
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
    QSpinBox::up-arrow {{
        image: url("{_up_arrow_svg}");
        width: 8px;
        height: 5px;
    }}
    QSpinBox::up-arrow:disabled {{
        image: url("{_up_arrow_svg_disabled}");
    }}
    QSpinBox::down-arrow {{
        image: url("{_down_arrow_svg}");
        width: 8px;
        height: 5px;
    }}
    QSpinBox::down-arrow:disabled {{
        image: url("{_down_arrow_svg_disabled}");
    }}
    QListWidget {{
        background: {GROUND_PANEL};
        border: 1px solid {BORDER_MED};
        border-radius: {RADIUS_SM}px;
        color: {TEXT_PRIMARY};
        outline: none;
    }}
    QListWidget::item {{
        padding: 6px 8px;
    }}
    QListWidget::item:selected {{
        background: {GROUND_SELECT};
        color: {TEXT_PRIMARY};
    }}
    QListWidget::item:hover {{
        background: {GROUND_INPUT};
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
    QTabWidget::pane {{
        border: 1px solid {BORDER_HAIRLINE};
        border-radius: {RADIUS_MD}px;
        background: {GROUND_PANEL};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_MUTED};
        padding: 8px 16px;
        margin-right: 2px;
        border: none;
        border-bottom: 2px solid transparent;
        font-weight: 600;
        font-size: 12px;
    }}
    QTabBar::tab:hover {{
        color: {TEXT_PRIMARY};
    }}
    QTabBar::tab:selected {{
        color: {ACCENT_GOLD};
        border-bottom: 2px solid {ACCENT_GOLD};
    }}
    QCheckBox {{
        color: {TEXT_SECONDARY};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {BORDER_STRONG};
        border-radius: 3px;
        background: {GROUND_INPUT};
    }}
    QCheckBox::indicator:checked {{
        background: {ACCENT_GOLD};
        border: 1px solid {ACCENT_GOLD};
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
            track = QColor(TOGGLE_TRACK_OFF)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        if not checked:
            # Extra definition beyond the lighter track fill alone — a thin
            # inner border so the switch's rounded-rect shape reads clearly
            # against a card background close in value to the track color.
            pen = QPen(QColor(255, 255, 255, 20))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.setPen(Qt.PenStyle.NoPen)

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


def themed_message_box(parent: QWidget | None = None) -> QMessageBox:
    """A ``QMessageBox`` that matches the gold/dark design system instead of
    the OS's native light chrome (critic R1+R2: delete/draft-guard dialogs
    were the only remaining native-light surface in the Settings window —
    QSS on the window doesn't reach a QMessageBox's own top-level palette,
    so it needs an explicit stylesheet of its own)."""
    box = QMessageBox(parent)
    box.setStyleSheet(f"""
        QMessageBox {{
            background: {GROUND_WINDOW};
            color: {TEXT_PRIMARY};
        }}
        QMessageBox QLabel {{
            color: {TEXT_PRIMARY};
        }}
        QMessageBox QPushButton {{
            background: transparent;
            border: 1px solid {BORDER_STRONG};
            color: {TEXT_SECONDARY};
            border-radius: {RADIUS_SM}px;
            padding: 6px 16px;
            min-width: 64px;
        }}
        QMessageBox QPushButton:hover {{
            background: rgba(255,255,255,0.05);
            color: {TEXT_PRIMARY};
        }}
        QMessageBox QPushButton:default {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {GOLD_GRAD_TOP}, stop:1 {GOLD_GRAD_BOTTOM});
            color: {GOLD_TEXT_ON};
            border: none;
            font-weight: 700;
        }}
    """)
    return box


def color_dot(color: str, parent: QWidget | None = None, size: int = 8) -> QWidget:
    """A small solid-color circle — a real painted widget, not the "●" text
    glyph (design review 2026-07-24 #4: that glyph tofus on fonts lacking it,
    e.g. the status-strip provider indicators and the footer's dirty dot)."""
    dot = QLabel(parent)
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return dot


def role_chip(label: str, color: str, parent: QWidget | None = None) -> QWidget:
    """Colored dot + label, matching the design system's role-chip component."""
    mono = ensure_fonts_loaded()["mono"]
    chip = QWidget(parent)
    lay = QHBoxLayout(chip)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    dot = color_dot(color, chip)
    lay.addWidget(dot)
    text = QLabel(label, chip)
    # Gemini #14 — chips/badges spec IBM Plex Mono; left unset here they
    # silently inherited the dialog's sans font instead.
    text.setStyleSheet(f'font-family: "{mono}"; color: {color}; font-weight: 600; font-size: 12px;')
    lay.addWidget(text)
    return chip


#: Horizontal padding + border of the `compact=True` gold_soft_chip, in px —
#: kept as a constant so callers that need to reserve layout space for the
#: chip (e.g. eliding a sibling label) can compute its width without a shown
#: widget (see settings_window._compact_chip_width).
COMPACT_CHIP_HPAD = 6 * 2
COMPACT_CHIP_BORDER = 1 * 2


def gold_soft_chip(text: str, parent: QWidget | None = None, *, compact: bool = False) -> QLabel:
    """The gold "soft chip" — e.g. the active-template badge in the status strip.

    ``compact=True`` shrinks padding/font-size for tight spaces (e.g. a
    QListWidget row) so it stops crowding out the sibling label's text."""
    mono = ensure_fonts_loaded()["mono"]
    chip = QLabel(text, parent)
    pad = f"1px {COMPACT_CHIP_HPAD // 2}px" if compact else "2px 10px"
    font_size = "10px" if compact else "11px"
    chip.setStyleSheet(
        f'font-family: "{mono}"; background: {GOLD_CHIP_BG}; border: 1px solid {GOLD_CHIP_BORDER};'
        f" border-radius: 999px; color: {GOLD_CHIP_TEXT}; padding: {pad};"
        f" font-size: {font_size}; font-weight: 600;"
    )
    return chip
