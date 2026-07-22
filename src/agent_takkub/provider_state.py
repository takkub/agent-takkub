"""Per-provider enable/disable state.

`provider_config.py` answers "which CLI backs role X" (per-role mapping).
This module answers "is provider Y currently usable" (per-provider gate) —
a different boundary, persisted in a different file, surfaced through a
different UI flow (status bar toggle, not config edit). Keep them apart.

State file: `~/.takkub/disabled-providers.json`
Format: `{"codex": true, "gemini": false}` — provider name → disabled flag
Missing file or corrupt JSON → treated as empty mapping (all enabled).

Persists across cockpit restart by design: user-level intent, not
session-scoped (see spec 2026-05-20-provider-toggle-design.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS_HOME

CODEX = "codex"
GEMINI = "gemini"


def _togglable() -> frozenset[str]:
    """Every registered provider except claude (the cockpit's baseline —
    disabling it would leave nothing to substitute with).

    Derived from PROVIDER_REGISTRY (#103 Phase 1) instead of a hand-maintained
    frozenset, so a new registry entry is automatically togglable in the
    status bar + Settings without an edit here. Lazy import keeps this module
    a thin config leaf at import time.
    """
    from .provider_spec import PROVIDER_REGISTRY

    return frozenset(PROVIDER_REGISTRY) - {"claude"}


# Providers that can be toggled. Adding a new togglable provider only needs a
# PROVIDER_REGISTRY entry; the status-bar chips (status_header) and Settings
# rows iterate this set. Update routing_planner only if the new provider gets
# provider-specific routing rules.
TOGGLABLE: frozenset[str] = _togglable()

_PATH = SETTINGS_HOME / "disabled-providers.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def load() -> dict[str, bool]:
    """Return current state mapping. Missing file or corrupt JSON → empty dict.

    Always returns a fresh dict — callers can mutate without side effects.
    """
    if not _PATH.exists():
        return {}
    try:
        raw = _PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Sanitize: drop entries with providers not in TOGGLABLE so a stale
    # entry from a previous build doesn't silently survive.
    return {str(k): bool(v) for k, v in data.items() if str(k) in TOGGLABLE}


def save(state: dict[str, bool]) -> None:
    """Persist `state` atomically. Drops keys not in TOGGLABLE."""
    cleaned = {str(k): bool(v) for k, v in state.items() if str(k) in TOGGLABLE}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)


def is_disabled(provider: str) -> bool:
    """True iff `provider` is currently disabled. Unknown providers → False."""
    return bool(load().get(provider, False))


def set_disabled(provider: str, flag: bool) -> None:
    """Flip `provider` to disabled (True) or enabled (False).

    Raises ValueError if `provider` is not in TOGGLABLE (catches typos
    at the call site rather than silently no-op'ing).
    """
    if provider not in TOGGLABLE:
        raise ValueError(f"unknown provider: {provider!r}")
    state = load()
    state[provider] = bool(flag)
    save(state)


def all_disabled() -> set[str]:
    """Return the set of provider names currently disabled."""
    return {k for k, v in load().items() if v}
