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
import time
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

    ``TAKKUB_V2_AUTHORITY`` (#362 Phase 10 wave 2, default off): when on and
    the dual-written ``v2/`` mirror exists, sanitizes THAT instead of the V1
    file. Falls back to V1 on any v2 miss.
    """
    from .core.storage.v2_authority import read_disabled_providers, v2_authority_enabled

    if v2_authority_enabled():
        v2_data = read_disabled_providers()
        if isinstance(v2_data, dict):
            return _sanitize(v2_data)

    if not _PATH.exists():
        return {}
    try:
        raw = _PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _sanitize(data)


def _sanitize(data: dict) -> dict[str, bool]:
    # Drop entries with providers not in TOGGLABLE so a stale entry from a
    # previous build doesn't silently survive.
    return {str(k): bool(v) for k, v in data.items() if str(k) in TOGGLABLE}


def save(state: dict[str, bool]) -> None:
    """Persist `state` atomically. Drops keys not in TOGGLABLE."""
    cleaned = {str(k): bool(v) for k, v in state.items() if str(k) in TOGGLABLE}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)

    from .core.storage.dual_write import dual_write_disabled_providers

    dual_write_disabled_providers(cleaned)


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


# ── quota-hit reroute state (#514) ──────────────────────────────────────────
# `limit_autoresume.py`'s AutoResumeMixin records here which provider is
# currently quota-hit and when its window resets, so the reroute picker can
# skip a provider that would just re-hit the same wall immediately.
#
# Deliberately NOT `providers/<provider>/<account>/...` (the per-account
# layout #504 will introduce) — #504 hasn't landed, so this is global-scope,
# same shape/location as `disabled-providers.json` above: one small JSON file
# under `~/.takkub/`, per-provider, no per-account split yet.
_QUOTA_PATH = SETTINGS_HOME / "provider-quota.json"


def quota_path() -> Path:
    """Where per-provider quota-reset state lives. Function form so tests
    can monkeypatch `_QUOTA_PATH`."""
    return _QUOTA_PATH


def load_quota_resets() -> dict[str, float]:
    """Return ``{provider: reset_at epoch}`` for providers currently
    recorded as quota-hit. Missing file or corrupt JSON -> empty dict
    (never blocks the reroute picker)."""
    path = quota_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _save_quota_resets(state: dict[str, float]) -> None:
    path = quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_quota_reset_at(provider: str, reset_at: float) -> None:
    """Record that `provider` is quota-hit until `reset_at` (epoch seconds).
    Overwrites any earlier recorded reset for the same provider."""
    state = load_quota_resets()
    state[provider] = float(reset_at)
    _save_quota_resets(state)


def clear_quota_reset(provider: str) -> None:
    """Drop `provider`'s recorded quota-hit — called once its window has
    actually reset. No-op if nothing was recorded."""
    state = load_quota_resets()
    if provider not in state:
        return
    del state[provider]
    _save_quota_resets(state)


def quota_reset_at(provider: str) -> float:
    """0.0 when `provider` has no recorded quota-hit."""
    return load_quota_resets().get(provider, 0.0)


def is_quota_ready(provider: str, now: float | None = None) -> bool:
    """True iff `provider` has no outstanding recorded quota-hit as of `now`
    (defaults to ``time.time()``) — i.e. safe to route new/rerouted work to."""
    if now is None:
        now = time.time()
    return quota_reset_at(provider) <= now
