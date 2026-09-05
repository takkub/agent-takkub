"""Live model-catalog merge for Settings' model pickers (#493).

`settings_window._MODELS_BY_PROVIDER` is a hand-maintained snapshot that
only gets re-verified when someone remembers to re-run each CLI's own
lister by hand — the exact gap that left the dropdown missing
gemini-3.7/gpt-5.6 for weeks after those shipped. `provider_model_refresh`
already has real, verified discovery for gemini/codex (used today to bump a
*pinned* model at boot); this module reuses those SAME functions to also
refresh the picker's OFFERED list, so there is one discovery implementation,
not two.

Design (simplest option that still refreshes automatically — see #493 task
note): a plain JSON cache under `config.RUNTIME_DIR`, keyed by provider, with
a `fetched_at` timestamp. Reading the cache (`cached_ids`) is just a small
file read — cheap enough to call inline while building a combo. Actually
running discovery (`refresh_cache`) shells out to the provider's CLI and
must stay off the Qt main thread; `settings_window` does that in a
background `QThread` kicked off once per Settings-window open, only when the
cache looks stale (`is_stale`) — a manual "refresh" button was considered
and rejected as the sole mechanism: a user who doesn't know the picker can
go stale would never know to press it, which is the actual #493 complaint.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import config, provider_model_refresh
from .provider_spec import PROVIDER_REGISTRY

logger = logging.getLogger(__name__)

# Mirrors provider_model_refresh's own `_DISCOVERY` keys — the only
# providers with a real, verified model-list mechanism this wave. kimi/
# cursor/opencode stay on the hand-maintained snapshot only (see
# provider_model_refresh.NO_MODEL_DISCOVERY_GAPS / #103 — tracked there, not
# duplicated here).
DISCOVERABLE_PROVIDERS: tuple[str, ...] = tuple(provider_model_refresh._DISCOVERY)

_CACHE_FILENAME = "model-catalog-cache.json"

# A new model line ships on the order of weeks; this just bounds how long a
# stale picker can persist between Settings opens, not a tight polling loop.
MAX_CACHE_AGE_S = 6 * 3600.0


def _cache_path():
    # `config.RUNTIME_DIR` read at call time (not module import), so a test
    # that monkeypatches it (see tests/test_settings_window.py's
    # _isolate_settings_paths) is honoured — a module-level constant would
    # have bound the original value at import and silently ignored the patch.
    return config.RUNTIME_DIR / _CACHE_FILENAME


def merge_catalog(snapshot: tuple[str, ...], discovered: list[str] | None) -> tuple[str, ...]:
    """*discovered* ids first (already in the CLI's own freshest-first
    order), then any *snapshot* preset not already covered — pure/no I/O, so
    this is unit-testable without Qt or a filesystem.

    `None`/empty *discovered* leaves *snapshot* untouched: that's both the
    "discovery hasn't run yet / failed" case here and, from
    `settings_window._fill_model_combo`'s call site, the permanent case for
    kimi/cursor/opencode (`cached_ids` always returns None for a provider
    with no discovery mechanism)."""
    if not discovered:
        return snapshot
    seen: set[str] = set()
    merged: list[str] = []
    for model_id in (*discovered, *snapshot):
        if model_id and model_id not in seen:
            seen.add(model_id)
            merged.append(model_id)
    return tuple(merged)


def _load_cache() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        logger.warning("model catalog cache: failed to write %s", path)


def cached_ids(provider: str) -> list[str] | None:
    """Whatever discovery last found for *provider*, however old — never
    blocks or shells out, just a small JSON read. Freshness is
    `is_stale`'s job, not this function's."""
    entry = _load_cache().get(provider)
    if not entry:
        return None
    ids = entry.get("ids")
    return list(ids) if isinstance(ids, list) and ids else None


def is_stale(provider: str, *, max_age_s: float = MAX_CACHE_AGE_S) -> bool:
    """True when *provider* has no cached catalog, or one older than
    *max_age_s* — including a provider with no discovery mechanism at all
    (always "stale", but `refresh_cache` below is a no-op for those, so
    nothing ever actually re-fetches on their behalf)."""
    entry = _load_cache().get(provider)
    if not entry:
        return True
    fetched_at = entry.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return True
    return (time.time() - fetched_at) > max_age_s


def refresh_cache(provider: str) -> list[str] | None:
    """Blocking: locates *provider*'s installed binary and runs its
    `provider_model_refresh` discovery function, persisting a hit. Caller's
    job to keep this off the Qt main thread (mirrors every other
    subprocess-backed provider probe in this codebase — see
    `provider_model_refresh._run`'s own callers).

    Returns None (and leaves the cache untouched) when the provider has no
    discovery mechanism, isn't installed, or discovery itself failed —
    never caches an empty/failed result, so a transient failure doesn't
    freeze a stale picker at "no options" until the next successful run."""
    if provider not in DISCOVERABLE_PROVIDERS:
        return None
    from . import provider_update

    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        return None
    binary = provider_update._discover(spec)
    if binary is None:
        return None
    discover = provider_model_refresh._discovery_for(provider)
    ids = discover(binary)
    if not ids:
        return None
    cache = _load_cache()
    cache[provider] = {"ids": ids, "fetched_at": time.time()}
    _save_cache(cache)
    return ids


def refresh_stale(providers: tuple[str, ...] = DISCOVERABLE_PROVIDERS) -> dict[str, list[str]]:
    """Blocking: `refresh_cache` every stale provider in *providers*,
    returning `{provider: ids}` for each that actually produced a fresh
    catalog. Meant to run entirely off the Qt main thread — see this
    module's docstring for why `settings_window` runs it in a `QThread`."""
    results: dict[str, list[str]] = {}
    for provider in providers:
        if not is_stale(provider):
            continue
        ids = refresh_cache(provider)
        if ids:
            results[provider] = ids
    return results
