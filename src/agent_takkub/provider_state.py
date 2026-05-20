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

CODEX = "codex"
GEMINI = "gemini"

# Providers that can be toggled. Adding a new togglable provider:
# (1) add to this frozenset, (2) add a chip in main_window status bar,
# (3) update routing_planner if it has provider-specific routing rules.
TOGGLABLE: frozenset[str] = frozenset({CODEX, GEMINI})

_PATH = Path.home() / ".takkub" / "disabled-providers.json"


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
    return {
        str(k): bool(v) for k, v in data.items() if str(k) in TOGGLABLE
    }


def save(state: dict[str, bool]) -> None:
    """Persist `state` atomically. Drops keys not in TOGGLABLE."""
    cleaned = {
        str(k): bool(v) for k, v in state.items() if str(k) in TOGGLABLE
    }
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
