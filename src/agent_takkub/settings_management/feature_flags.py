"""Resolve which Settings surface opens — ``legacy`` (default), ``new``, or
``compare`` (both, dev-only) — from ``TAKKUB_SETTINGS_UI``.

Single resolution point (SPEC.md "Coexistence") — every entry point (the
status-bar Settings button, this package's ``__main__``, tests) reads
``resolve()`` instead of the env var directly, so a future config-file-backed
override only needs to change this one function.
"""

from __future__ import annotations

import os
from enum import StrEnum

_ENV_VAR = "TAKKUB_SETTINGS_UI"


class SettingsUI(StrEnum):
    LEGACY = "legacy"
    NEW = "new"
    COMPARE = "compare"


def resolve() -> SettingsUI:
    """Read ``TAKKUB_SETTINGS_UI`` (case-insensitive). Unset/unknown -> LEGACY."""
    raw = (os.environ.get(_ENV_VAR) or "").strip().lower()
    try:
        return SettingsUI(raw)
    except ValueError:
        return SettingsUI.LEGACY
