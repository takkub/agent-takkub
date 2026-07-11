"""Ownership pill — ``BUILT-IN`` / ``CUSTOM`` / ``MANAGED`` / ``EXTERNAL`` /
``PROJECT``. Same visual family as ``cockpit_theme.gold_soft_chip`` but with
a per-ownership tint so read-only vs editable is legible without opening the
detail pane (SPEC.md "Read-only affordance")."""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QWidget

from ... import cockpit_theme as theme
from ..models import Ownership

# Ownership -> (bg, border, text) — gold for the one ownership that means
# "you can freely edit/delete this", muted/neutral for the read-only ones.
_TINTS: dict[Ownership, tuple[str, str, str]] = {
    Ownership.CUSTOM: (theme.GOLD_CHIP_BG, theme.GOLD_CHIP_BORDER, theme.GOLD_CHIP_TEXT),
    Ownership.BUILT_IN: (theme.NEUTRAL_CHIP_BG, theme.NEUTRAL_CHIP_BORDER, theme.NEUTRAL_CHIP_TEXT),
    Ownership.MANAGED: (theme.NEUTRAL_CHIP_BG, theme.NEUTRAL_CHIP_BORDER, theme.NEUTRAL_CHIP_TEXT),
    Ownership.EXTERNAL: (theme.NEUTRAL_CHIP_BG, theme.NEUTRAL_CHIP_BORDER, theme.TEXT_MUTED),
    Ownership.PROJECT: (theme.GOLD_CHIP_BG, theme.GOLD_CHIP_BORDER, theme.GOLD_CHIP_TEXT),
}


def make_source_badge(ownership: Ownership, parent: QWidget | None = None) -> QLabel:
    bg, border, text_color = _TINTS[ownership]
    label = QLabel(ownership.value.upper(), parent)
    label.setObjectName("sourceBadge")
    label.setStyleSheet(
        f"QLabel#sourceBadge {{"
        f"background: {bg}; border: 1px solid {border}; color: {text_color};"
        f"border-radius: 999px; padding: 2px 10px; font-weight: 600;"
        f"}}"
    )
    return label
