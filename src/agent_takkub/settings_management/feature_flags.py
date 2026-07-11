"""Resolve which Settings surface opens — ``legacy`` (default) or ``new`` —
from ``TAKKUB_SETTINGS_UI``.

Default rolled back to LEGACY (2026-07-11 evening): the new surface passed
critic review but the actual user rejected it in real use ("ใช้ยากกว่าเดิม")
— the flag exists precisely so this is a one-line revert. The new window
stays fully functional behind ``TAKKUB_SETTINGS_UI=new`` while its UX is
reworked against the user's actual complaints.

Single resolution point (SPEC.md "Coexistence") — every entry point (the
status-bar Settings button, this package's ``__main__``, tests) reads
``resolve()`` instead of the env var directly, so a future config-file-backed
override only needs to change this one function.

MED-4 (2026-07-11 codex cross-check): the original design sketched a third
``compare`` value ("both, dev-only"), but the only router
(``user_actions._open_settings_window``) never special-cased it — every
value except ``new`` opened legacy only, so ``compare`` silently behaved
like ``legacy`` while the docs claimed it opened both surfaces. Actually
opening both windows side by side is a real feature, not a one-line fix —
it belongs with whoever owns the window shell (``window.py``/
``user_actions.py``, out of this change's scope), so ``compare`` is
dropped here rather than left promising behavior that doesn't exist. Any
unrecognized value (including a stale ``compare`` in an existing env)
falls back to NEW, same as any other unknown value.
"""

from __future__ import annotations

import os
from enum import StrEnum

_ENV_VAR = "TAKKUB_SETTINGS_UI"


class SettingsUI(StrEnum):
    LEGACY = "legacy"
    NEW = "new"


def resolve() -> SettingsUI:
    """Read ``TAKKUB_SETTINGS_UI`` (case-insensitive). Unset/unknown -> LEGACY."""
    raw = (os.environ.get(_ENV_VAR) or "").strip().lower()
    try:
        return SettingsUI(raw)
    except ValueError:
        return SettingsUI.LEGACY
