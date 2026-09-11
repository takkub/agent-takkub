"""Design tokens + QSS/widget helpers for the whole Takkub Cockpit UI.

Source of truth (2026-09-08 redesign): `docs/design-review/
2026-09-08-cockpit-ui-e2e-critic-and-redesign-spec.md` +
`2026-09-08-cockpit-ui-audit-redesign-gemini.md` — the gold `#E3B341` +
IBM Plex Mono-for-labels system was replaced end to end with an Indigo
`#6366F1` (dark) / `#4F46E5` (light) accent + slate neutrals, WCAG
AA-checked. The `GOLD_*`/`ACCENT_GOLD*` constant NAMES were kept as-is
(renaming every call site across ~15 modules was judged not worth the
blast radius for a values-only redesign) — they now hold Indigo hex, not
gold. Read them as "primary accent", not literally "gold". Superseded:
`docs/design-review/2026-07-10-cockpit-settings-design-system.md` (the
original gold system this replaces).

Theme variants (#506): the module-level constants below ARE the live token
values — they default to the dark set and are rebound in place by
``apply_variant("light"|"dark")`` (see the "Theme variants" section near the
bottom). Consumers must import the *module* (``from . import cockpit_theme``)
and read attributes at style-build time, never ``from .cockpit_theme import
ACCENT_GOLD`` — a from-import freezes the dark value forever. The persisted
mode + OS detection live in :mod:`theme_settings`; this module deliberately
never imports it so it stays a leaf.

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
    QDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

_log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Grounds — "Deep Obsidian" (2026-09-08 redesign)
# ──────────────────────────────────────────────────────────────
GROUND_BODY = "#08090D"
GROUND_WINDOW = "#111318"
GROUND_TITLEBAR = "#0B0C10"
STATUS_STRIP_GRAD_TOP = "#171A21"
STATUS_STRIP_GRAD_BOTTOM = "#121419"
GROUND_SIDEBAR = "#0D0F14"
GROUND_PANEL = "#181B22"
GROUND_PANEL_ALT = "#1C2029"
GROUND_INPUT = "#0F172A"
GROUND_SELECT = "#222630"
# Nested-card ground one shade darker than GROUND_PANEL — the boot-flow
# wizard's provider-list / backup-item cards (#574) sit on GROUND_PANEL and
# need their own fill distinct from both that and GROUND_INPUT.
GROUND_INSET = "#14171E"
# ToggleSwitch's unchecked track — deliberately lighter than GROUND_SELECT.
# GROUND_SELECT against card background GROUND_PANEL had almost no delta, so
# an off toggle's rounded-rect shape barely read against the card behind it
# (design review 2026-07-24 #4, gemini + critic both flagged it).
TOGGLE_TRACK_OFF = "#2D3341"
# ToggleSwitch's off-track inner border, as QColor RGBA components (painted,
# not QSS) — the white overlay that defines the shape on dark needs to flip
# to a dark overlay on light grounds.
TOGGLE_TRACK_EDGE_RGBA: tuple[int, int, int, int] = (255, 255, 255, 20)

# ──────────────────────────────────────────────────────────────
# Borders
# ──────────────────────────────────────────────────────────────
BORDER_HAIRLINE = "rgba(255,255,255,0.07)"
BORDER_MED = "rgba(255,255,255,0.10)"
BORDER_STRONG = "rgba(255,255,255,0.12)"
BORDER_STRONG2 = "rgba(255,255,255,0.14)"
# Solid-hex card border (boot-flow wizard, #574) — distinct from the
# rgba-over-ground overlays above because it must render the same fixed
# hue over both GROUND_PANEL and GROUND_INSET cards in one dialog.
BORDER_CARD = "#262B36"
# Divider between rows inside a GROUND_INSET card — one step lighter than
# BORDER_CARD so nested rows read as grouped, not as separate cards.
BORDER_CARD_ROW = "#1F232C"
# Outline for an inert/disabled control (an already-up-to-date row's
# checkbox, a not-yet-reached phase dot) on a GROUND_INPUT ground.
BORDER_CONTROL = "#3A4150"
# Hover washes — a translucent overlay of the *text* pole (white on dark,
# near-black on light), so the same QSS reads correctly in both variants.
HOVER_FAINT = "rgba(255,255,255,0.04)"
HOVER_WEAK = "rgba(255,255,255,0.05)"
RADIUS_SM = 8
RADIUS_MD = 10
RADIUS_LG = 14

# ──────────────────────────────────────────────────────────────
# Accent — Electric Indigo (2026-09-08 redesign; names kept as GOLD_* /
# ACCENT_GOLD*, see module docstring — values are Indigo, not gold).
# ──────────────────────────────────────────────────────────────
ACCENT_GOLD = "#6366F1"
# Accent used as *text/glyph color* on a plain ground (contentPreTitle,
# selected tab). Same as ACCENT_GOLD in dark; the light variant darkens it
# further than the fill accent so small accent text still passes contrast on
# light grounds.
ACCENT_GOLD_TEXT = "#6366F1"
GOLD_GRAD_TOP = "#7B7FF4"
GOLD_GRAD_BOTTOM = "#6366F1"
GOLD_GRAD_HOVER_TOP = "#8B8EF6"
GOLD_TEXT_ON = "#FFFFFF"
GOLD_CHIP_BG = "rgba(99,102,241,0.16)"
GOLD_CHIP_BG_HOVER = "rgba(99,102,241,0.22)"
GOLD_CHIP_BORDER = "rgba(99,102,241,0.4)"
GOLD_CHIP_TEXT = "#A5A6F5"

# ──────────────────────────────────────────────────────────────
# Text
# ──────────────────────────────────────────────────────────────
TEXT_PRIMARY = "#F3F4F6"
TEXT_PRIMARY_ALT = "#F8FAFC"
TEXT_SECONDARY = "#9CA3AF"
TEXT_SECONDARY_ALT = "#A6ADB8"
TEXT_MUTED = "#6B7280"
TEXT_MUTED_ALT = "#7C8493"
TEXT_FAINT = "#4B5563"
TEXT_FAINT_ALT = "#525A66"

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
PROVIDER_CLAUDE = "#d97757"  # Anthropic clay — same brand hue as METER_CLAY
PROVIDER_CODEX = "#10a37f"  # OpenAI teal
PROVIDER_GEMINI = "#4285f4"  # Google blue
PROVIDER_OPENCODE = "#f97316"  # sst orange
PROVIDER_KIMI = "#6366f1"  # Moonshot indigo
PROVIDER_CURSOR = "#38bdf8"  # Cursor sky-blue

# ──────────────────────────────────────────────────────────────
# State colors — status semantics (ok/warn/error/info). The *meaning* is
# intentional and survives migration: never turn these gold, only tokenize
# the value. `_BRIGHT` variants are for small dots/glyphs on dark grounds
# where the base ramp reads too dim (task_dock / agent_pane status dots).
# ──────────────────────────────────────────────────────────────
STATE_OK = "#10B981"
STATE_WARN = "#F59E0B"
STATE_ERROR = "#ef4444"
STATE_INFO = "#4E86F7"
STATE_OK_BRIGHT = "#22c55e"
STATE_WARN_BRIGHT = "#facc15"
STATE_ERROR_BRIGHT = "#f87171"
STATE_INFO_BRIGHT = "#0ea5e9"
# Muted error-icon roundel fill (boot-flow wizard's failed-page icon, #574) —
# duller than STATE_ERROR/BANNER_ERROR_BG so a 36px icon circle reads as
# "handled, not alarming" (rollback already ran) rather than a live alert.
STATE_ERROR_ICON_BG = "#3a2226"
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

# Context-usage fill ramp (token_meter.usage_color) — neutral → warn → high →
# critical. Dark values are the historical literals; light overrides darken
# them so the badge text stays readable on light grounds.
USAGE_NEUTRAL = "#9ca3af"
USAGE_WARN = "#facc15"
USAGE_HIGH = "#f97316"
USAGE_CRIT = "#ef4444"

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
# this dict falls back to its own Role.color. codex/gemini/opencode/kimi/
# cursor reuse the PROVIDER_* brand tokens; shell is a neutral slate.
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
    "opencode": PROVIDER_OPENCODE,
    "kimi": PROVIDER_KIMI,
    "cursor": PROVIDER_CURSOR,
    "tester": "#B5D33D",
}

# ──────────────────────────────────────────────────────────────
# Theme variants (#506) — the constants above are the DARK set and double as
# the live values. `apply_variant()` rebinds them in place, so every module
# that reads `cockpit_theme.X` at style-build time follows the switch; only
# stylesheets already applied to a live widget need a `retheme()` re-apply
# (see `retheme_open_windows`). Identity colors (ROLE_COLORS, PROVIDER_*,
# AVATAR_TINTS) are deliberately NOT themed — a role/provider keeps one hue
# in both variants.
# ──────────────────────────────────────────────────────────────

_THEMED_TOKEN_NAMES: tuple[str, ...] = (
    # grounds
    "GROUND_BODY",
    "GROUND_WINDOW",
    "GROUND_TITLEBAR",
    "STATUS_STRIP_GRAD_TOP",
    "STATUS_STRIP_GRAD_BOTTOM",
    "GROUND_SIDEBAR",
    "GROUND_PANEL",
    "GROUND_PANEL_ALT",
    "GROUND_INPUT",
    "GROUND_SELECT",
    "GROUND_INSET",
    "TOGGLE_TRACK_OFF",
    "TOGGLE_TRACK_EDGE_RGBA",
    # borders / hovers
    "BORDER_HAIRLINE",
    "BORDER_MED",
    "BORDER_STRONG",
    "BORDER_STRONG2",
    "BORDER_CARD",
    "BORDER_CARD_ROW",
    "BORDER_CONTROL",
    "HOVER_FAINT",
    "HOVER_WEAK",
    # gold accent
    "ACCENT_GOLD",
    "ACCENT_GOLD_TEXT",
    "GOLD_GRAD_TOP",
    "GOLD_GRAD_BOTTOM",
    "GOLD_GRAD_HOVER_TOP",
    "GOLD_TEXT_ON",
    "GOLD_CHIP_BG",
    "GOLD_CHIP_BG_HOVER",
    "GOLD_CHIP_BORDER",
    "GOLD_CHIP_TEXT",
    # text
    "TEXT_PRIMARY",
    "TEXT_PRIMARY_ALT",
    "TEXT_SECONDARY",
    "TEXT_SECONDARY_ALT",
    "TEXT_MUTED",
    "TEXT_MUTED_ALT",
    "TEXT_FAINT",
    "TEXT_FAINT_ALT",
    # badges / chips
    "SUBSTITUTE_BADGE_TEXT",
    "SUBSTITUTE_BADGE_BORDER",
    "PARALLEL_CHIP_BG",
    "PARALLEL_CHIP_BORDER",
    "PARALLEL_CHIP_TEXT",
    "NEUTRAL_CHIP_BG",
    "NEUTRAL_CHIP_BORDER",
    "NEUTRAL_CHIP_TEXT",
    "ERROR_CHIP_BG",
    "ERROR_CHIP_BORDER",
    "ERROR_CHIP_TEXT",
    # state colors
    "STATE_OK",
    "STATE_WARN",
    "STATE_ERROR",
    "STATE_INFO",
    "STATE_OK_BRIGHT",
    "STATE_WARN_BRIGHT",
    "STATE_ERROR_BRIGHT",
    "STATE_INFO_BRIGHT",
    "STATE_ERROR_ICON_BG",
    "STATE_EXITED",
    "STATE_WARN_ALT",
    # status-bar chip identities
    "CHIP_EXEC_PARALLEL",
    "CHIP_REMOTE_ON",
    "ROLE_COLOR_FALLBACK",
    # meters / usage ramp
    "METER_CLAY",
    "METER_CLAY_ALT",
    "METER_AMBER",
    "METER_AMBER_LIGHT",
    "USAGE_NEUTRAL",
    "USAGE_WARN",
    "USAGE_HIGH",
    "USAGE_CRIT",
    # banner triples
    "BANNER_WARN_BG",
    "BANNER_WARN_BORDER",
    "BANNER_WARN_TEXT",
    "BANNER_WARN_HOVER",
    "BANNER_OK_BG",
    "BANNER_OK_BORDER",
    "BANNER_OK_TEXT",
    "BANNER_OK_HOVER",
    "BANNER_ERROR_BG",
    "BANNER_ERROR_BORDER",
    "BANNER_ERROR_TEXT",
    "BANNER_INFO_BG",
    "BANNER_INFO_BORDER",
    "BANNER_INFO_TEXT",
    "BANNER_INFO_HOVER",
)

# Snapshot of the dark values above, taken at import time — the constants ARE
# the dark set, so this never drifts from them.
DARK_TOKENS: dict[str, object] = {name: globals()[name] for name in _THEMED_TOKEN_NAMES}

# Light variant — NOT a naive inversion. Grounds go paper-light with white
# cards; borders/hovers flip to dark overlays; the accent darkens to
# #4F46E5 (8.2:1 vs white) and accent-as-*text* darkens further (#4338CA);
# state/chip hues shift to their dark-on-light equivalents so small colored
# text stays readable. Replaces the old gold system's "muddy mustard" light
# variant (#a87b16/#7a5a10 — 2026-09-08 redesign, design-review audit).
LIGHT_TOKENS: dict[str, object] = {
    # grounds — "Pure Crisp Slate"
    "GROUND_BODY": "#E2E8F0",
    "GROUND_WINDOW": "#F8FAFC",
    "GROUND_TITLEBAR": "#EEF2F7",
    "STATUS_STRIP_GRAD_TOP": "#FFFFFF",
    "STATUS_STRIP_GRAD_BOTTOM": "#F1F5F9",
    "GROUND_SIDEBAR": "#F1F5F9",
    "GROUND_PANEL": "#FFFFFF",
    "GROUND_PANEL_ALT": "#F8FAFC",
    "GROUND_INPUT": "#F8FAFC",
    "GROUND_SELECT": "#E2E8F0",
    "GROUND_INSET": "#F1F5F9",
    "TOGGLE_TRACK_OFF": "#CBD5E1",
    "TOGGLE_TRACK_EDGE_RGBA": (16, 24, 40, 36),
    # borders / hovers
    "BORDER_HAIRLINE": "rgba(16,24,40,0.10)",
    "BORDER_MED": "rgba(16,24,40,0.14)",
    "BORDER_STRONG": "rgba(16,24,40,0.20)",
    "BORDER_STRONG2": "rgba(16,24,40,0.24)",
    "BORDER_CARD": "#E2E8F0",
    "BORDER_CARD_ROW": "#EDF1F6",
    "BORDER_CONTROL": "#CBD5E1",
    "HOVER_FAINT": "rgba(16,24,40,0.04)",
    "HOVER_WEAK": "rgba(16,24,40,0.06)",
    # accent — Deep Indigo
    "ACCENT_GOLD": "#4F46E5",
    "ACCENT_GOLD_TEXT": "#4338CA",
    "GOLD_GRAD_TOP": "#5B54EE",
    "GOLD_GRAD_BOTTOM": "#4F46E5",
    "GOLD_GRAD_HOVER_TOP": "#4338CA",
    "GOLD_TEXT_ON": "#FFFFFF",
    "GOLD_CHIP_BG": "rgba(79,70,229,0.10)",
    "GOLD_CHIP_BG_HOVER": "rgba(79,70,229,0.16)",
    "GOLD_CHIP_BORDER": "rgba(79,70,229,0.35)",
    "GOLD_CHIP_TEXT": "#4338CA",
    # text
    "TEXT_PRIMARY": "#0F172A",
    "TEXT_PRIMARY_ALT": "#1E293B",
    "TEXT_SECONDARY": "#475569",
    "TEXT_SECONDARY_ALT": "#52607A",
    "TEXT_MUTED": "#64748B",
    "TEXT_MUTED_ALT": "#5D6B85",
    "TEXT_FAINT": "#94A3B8",
    "TEXT_FAINT_ALT": "#8592A8",
    # badges / chips
    "SUBSTITUTE_BADGE_TEXT": "#a34d21",
    "SUBSTITUTE_BADGE_BORDER": "rgba(163,77,33,0.45)",
    "PARALLEL_CHIP_BG": "rgba(124,58,237,0.10)",
    "PARALLEL_CHIP_BORDER": "rgba(124,58,237,0.35)",
    "PARALLEL_CHIP_TEXT": "#6d28d9",
    "NEUTRAL_CHIP_BG": "rgba(16,24,40,0.06)",
    "NEUTRAL_CHIP_BORDER": "rgba(16,24,40,0.20)",
    "NEUTRAL_CHIP_TEXT": "#3d4450",
    "ERROR_CHIP_BG": "rgba(185,28,28,0.08)",
    "ERROR_CHIP_BORDER": "rgba(185,28,28,0.35)",
    "ERROR_CHIP_TEXT": "#b91c1c",
    # state colors
    "STATE_OK": "#059669",
    "STATE_WARN": "#D97706",
    "STATE_ERROR": "#dc2626",
    "STATE_INFO": "#2563eb",
    "STATE_OK_BRIGHT": "#16a34a",
    "STATE_WARN_BRIGHT": "#b45309",
    "STATE_ERROR_BRIGHT": "#dc2626",
    "STATE_INFO_BRIGHT": "#0369a1",
    "STATE_ERROR_ICON_BG": "#fee2e2",
    "STATE_EXITED": "#c2410c",
    "STATE_WARN_ALT": "#b45309",
    # status-bar chip identities
    "CHIP_EXEC_PARALLEL": "#047857",
    "CHIP_REMOTE_ON": "#0f766e",
    "ROLE_COLOR_FALLBACK": "#64748b",
    # meters / usage ramp
    "METER_CLAY": "#c05a34",
    "METER_CLAY_ALT": "#b04e2a",
    "METER_AMBER": "#d97706",
    "METER_AMBER_LIGHT": "#b45309",
    "USAGE_NEUTRAL": "#6b7280",
    "USAGE_WARN": "#b45309",
    "USAGE_HIGH": "#c2410c",
    "USAGE_CRIT": "#dc2626",
    # banner triples
    "BANNER_WARN_BG": "#fef3c7",
    "BANNER_WARN_BORDER": "#d97706",
    "BANNER_WARN_TEXT": "#92400e",
    "BANNER_WARN_HOVER": "#fde68a",
    "BANNER_OK_BG": "#dcfce7",
    "BANNER_OK_BORDER": "#16a34a",
    "BANNER_OK_TEXT": "#166534",
    "BANNER_OK_HOVER": "#bbf7d0",
    "BANNER_ERROR_BG": "#fee2e2",
    "BANNER_ERROR_BORDER": "#dc2626",
    "BANNER_ERROR_TEXT": "#991b1b",
    "BANNER_INFO_BG": "#dbeafe",
    "BANNER_INFO_BORDER": "#2563eb",
    "BANNER_INFO_TEXT": "#1e40af",
    "BANNER_INFO_HOVER": "#bfdbfe",
}

_current_variant = "dark"


def current_variant() -> str:
    """The variant whose tokens are currently bound: ``"dark"`` or ``"light"``."""
    return _current_variant


def apply_variant(variant: str) -> None:
    """Rebind every themed module-level token to *variant*'s value set.

    Takes effect for any stylesheet built AFTER this call; stylesheets already
    applied to live widgets keep rendering the old values until re-applied —
    call :func:`retheme_open_windows` right after to refresh long-lived
    windows immediately.
    """
    global _current_variant
    if variant not in ("dark", "light"):
        raise ValueError(f"unknown theme variant: {variant!r}")
    globals().update(DARK_TOKENS if variant == "dark" else LIGHT_TOKENS)
    _current_variant = variant


def retheme_open_windows() -> None:
    """Ask every open top-level widget that exposes a ``retheme()`` hook to
    re-apply its stylesheet with the currently-bound tokens. Windows without
    the hook are untouched (transient dialogs pick up the new tokens on their
    next construction anyway)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for widget in QApplication.topLevelWidgets():
        hook = getattr(widget, "retheme", None)
        if callable(hook):
            try:
                hook()
            except Exception:
                _log.exception("retheme() hook failed for %r", widget)


# ──────────────────────────────────────────────────────────────
# Fonts — bundled IBM Plex (OFL, github.com/IBM/plex) with a graceful
# cross-platform fallback. Never blocks boot: a missing/corrupt ttf just
# means the fallback family is used and `ensure_fonts_loaded()["bundled"]`
# comes back False so the caller can flag it.
# ──────────────────────────────────────────────────────────────
FONT_SANS_FALLBACK_CANDIDATES: tuple[str, ...] = (
    "Segoe UI Variable",  # Windows 11 (2026-09-08 redesign preference)
    "Segoe UI",  # Windows 10 fallback
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
    # SVG fills are baked into the files, so each variant ships its own set
    # (the dark arrows are near-invisible on light inputs and vice versa).
    _suffix = "-light" if _current_variant == "light" else ""
    _up_arrow_svg = (_icons_dir / f"spin-up{_suffix}.svg").as_posix()
    _down_arrow_svg = (_icons_dir / f"spin-down{_suffix}.svg").as_posix()
    _up_arrow_svg_disabled = (_icons_dir / f"spin-up-disabled{_suffix}.svg").as_posix()
    _down_arrow_svg_disabled = (_icons_dir / f"spin-down-disabled{_suffix}.svg").as_posix()
    _combo_arrow_on_svg = (_icons_dir / f"combo-down-on{_suffix}.svg").as_posix()
    _sans_stack = _sans_font_stack(sans_family)
    return f"""
    QDialog#settingsWindow, QWidget#settingsWindow, QDialog#cockpitDialog {{
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
        font-family: {_sans_stack};
        color: {TEXT_FAINT};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1.5px;
        padding: 12px 14px 4px 14px;
    }}
    QPushButton#sidebarSectionToggle {{
        font-family: {_sans_stack};
        color: {TEXT_FAINT};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1.5px;
        text-align: left;
        padding: 12px 14px 4px 14px;
        border: none;
        background: transparent;
    }}
    QPushButton#sidebarSectionToggle:hover {{
        color: {TEXT_SECONDARY};
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
        background: {HOVER_FAINT};
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
    QWidget#content {{
        background: {GROUND_WINDOW};
    }}
    QLabel#contentPreTitle {{
        font-family: {_sans_stack};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1.5px;
        color: {ACCENT_GOLD_TEXT};
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
            stop:0 {GOLD_GRAD_HOVER_TOP}, stop:1 {GOLD_GRAD_TOP});
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
        background: {HOVER_WEAK};
        color: {TEXT_PRIMARY};
    }}
    QPushButton#secondaryButton:checked {{
        background: {ACCENT_GOLD};
        border: 1px solid {ACCENT_GOLD};
        color: {GOLD_TEXT_ON};
    }}
    QPushButton#secondaryButton:disabled {{
        color: {TEXT_FAINT};
        border: 1px solid {BORDER_HAIRLINE};
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
        font-size: 14px;
        color: {TEXT_PRIMARY};
    }}
    QLabel#panelHint {{
        color: {TEXT_MUTED};
        font-size: 13px;
    }}
    QWidget#providerRow, QWidget#roleRow {{
        background: {GROUND_PANEL_ALT};
        border: 1px solid {BORDER_HAIRLINE};
        border-radius: {RADIUS_SM}px;
    }}
    QLabel#matrixHeaderCell {{
        font-family: {_sans_stack};
        font-weight: 600;
        font-size: 12px;
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
    /* No placeholder color was ever set here, so Qt fell back to its own
       default (a faint tint derived from the palette) — nearly unreadable
       against GROUND_INPUT (#1c1f26), confirmed by rendered screenshot
       (docs/design/2026-08-13-new-role-critique.md). QPlainTextEdit has no
       ::placeholder selector in Qt6 — it never needed one, since it never
       showed a placeholder via stylesheet in the first place; only
       QLineEdit does. */
    QLineEdit::placeholder {{
        color: {TEXT_MUTED};
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
        background: {HOVER_WEAK};
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
        color: {ACCENT_GOLD_TEXT};
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
            pen = QPen(QColor(*TOGGLE_TRACK_EDGE_RGBA))
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
    glow_color = QColor(ACCENT_GOLD)
    glow_color.setAlpha(153)
    glow.setColor(glow_color)
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
            background: {HOVER_WEAK};
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


class CockpitDialog(QDialog):
    """Base class for every standalone (non-Settings-window) dialog.

    A plain ``QDialog(parent)`` does NOT inherit the parent window's QSS —
    Qt style sheets only cascade to child *widgets* inside the same
    top-level window, and a ``QDialog`` is always its own top-level window
    even when parented. Every raw ``QDialog`` in this codebase (Add
    Account, Remote Settings, Map Paths, Rules Editor, New Project's picker)
    rendered with the OS's native light chrome regardless of the active
    theme because of this — the "blinding white dialog in dark mode" /
    "invisible Cancel button" bugs from the 2026-09-08 design review.
    Subclass this instead of ``QDialog`` (or swap an existing ``QDialog``
    subclass's base) to fix it; call :meth:`retheme` again after
    :func:`apply_variant` to follow a live theme switch (or rely on
    :func:`retheme_open_windows`, which calls it automatically for every
    open top-level widget that has the hook)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cockpitDialog")
        self.retheme()

    def retheme(self) -> None:
        fonts = ensure_fonts_loaded()
        self.setStyleSheet(build_stylesheet(str(fonts["sans"]), str(fonts["mono"])))


def color_dot(color: str, parent: QWidget | None = None, size: int = 8) -> QWidget:
    """A small solid-color circle — a real painted widget, not the "●" text
    glyph (design review 2026-07-24 #4: that glyph tofus on fonts lacking it,
    e.g. the status-strip provider indicators and the footer's dirty dot)."""
    dot = QLabel(parent)
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return dot


def role_chip(label: str, color: str, parent: QWidget | None = None) -> QWidget:
    """Colored dot + label, matching the design system's role-chip component.

    Uses the sans stack, not mono (2026-09-08 redesign) — *label* is a human
    role/name string, not code/an identifier, and mono was flagged for
    causing premature truncation of longer role/template names."""
    sans = ensure_fonts_loaded()["sans"]
    chip = QWidget(parent)
    lay = QHBoxLayout(chip)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    dot = color_dot(color, chip)
    lay.addWidget(dot)
    text = QLabel(label, chip)
    text.setStyleSheet(f'font-family: "{sans}"; color: {color}; font-weight: 600; font-size: 13px;')
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
    QListWidget row) so it stops crowding out the sibling label's text.

    Sans, not mono (2026-09-08 redesign) — chip text is usually a human
    template/project name, and mono caused premature truncation."""
    sans = ensure_fonts_loaded()["sans"]
    chip = QLabel(text, parent)
    pad = f"1px {COMPACT_CHIP_HPAD // 2}px" if compact else "2px 10px"
    font_size = "10px" if compact else "11px"
    chip.setStyleSheet(
        f'font-family: "{sans}"; background: {GOLD_CHIP_BG}; border: 1px solid {GOLD_CHIP_BORDER};'
        f" border-radius: 999px; color: {GOLD_CHIP_TEXT}; padding: {pad};"
        f" font-size: {font_size}; font-weight: 600;"
    )
    return chip
