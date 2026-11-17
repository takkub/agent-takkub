"""Persist the optional CLI model selected for each provider.

State file: ``v2/models/registry.json`` under the cockpit's data home (#504
cut half — direct V2 read/write, no V1 file, no dual-write mirror).
Missing, corrupt, or stale entries are treated as absent so each provider
falls back to its own CLI default unless the user explicitly selects a
model.
"""

from __future__ import annotations

from pathlib import Path


def _target() -> Path:
    from .core.storage.layout import storage_layout_v2
    from .core.storage.v2_target import effective_data_home

    home = effective_data_home(None, prefer_primary=True)
    return storage_layout_v2(home).models / "registry.json"


def path() -> Path:
    """Where model selections live (the V2 target). Function form lets
    tests patch it."""
    return _target()


def _providers() -> frozenset[str]:
    """Return registered provider names without creating an import cycle."""
    from .provider_spec import PROVIDER_REGISTRY

    return frozenset(PROVIDER_REGISTRY)


def _load() -> dict[str, str]:
    """Load and sanitize configured models; invalid state behaves as empty."""
    from .core.storage.v2_target import read_data

    data = read_data(path())
    if not isinstance(data, dict):
        return {}
    return _sanitize(data)


def _sanitize(data: dict) -> dict[str, str]:
    providers = _providers()
    cleaned: dict[str, str] = {}
    for key, value in data.items():
        provider = str(key)
        if provider not in providers or not isinstance(value, str):
            continue
        model = value.strip()
        if model:
            cleaned[provider] = model
    return cleaned


def _save(models: dict[str, str]) -> None:
    """Persist model selections atomically, dropping stale provider keys."""
    providers = _providers()
    cleaned = {
        str(provider): model.strip()
        for provider, model in models.items()
        if str(provider) in providers and isinstance(model, str) and model.strip()
    }
    from .core.storage.v2_target import write_data

    write_data(path(), cleaned)


def model_for(provider: str) -> str | None:
    """Return the configured model, or ``None`` to use the provider default."""
    return _load().get(provider)


def set_model(provider: str, model: str) -> None:
    """Set a provider model. Whitespace-only values clear the selection."""
    if provider not in _providers():
        raise ValueError(f"unknown provider: {provider!r}")
    normalized = model.strip()
    if not normalized:
        clear_model(provider)
        return
    models = _load()
    models[provider] = normalized
    _save(models)


def clear_model(provider: str) -> None:
    """Clear a provider model so its CLI default is used."""
    if provider not in _providers():
        raise ValueError(f"unknown provider: {provider!r}")
    models = _load()
    models.pop(provider, None)
    _save(models)


# ── fallback model (#1031) ───────────────────────────────────────────────────
# The model a provider's CLI switches a session to when its own model is
# overloaded. Kept beside the registry in the same `models/` folder, in its own
# file: `registry.json` is a flat provider→model map that every reader
# sanitises by provider name, so a second kind of value cannot share it.


def fallback_path() -> Path:
    return path().with_name("fallback.json")


def _load_fallbacks() -> dict[str, str]:
    from .core.storage.v2_target import read_data

    data = read_data(fallback_path())
    return _sanitize(data) if isinstance(data, dict) else {}


def fallback_for(provider: str) -> str | None:
    """The configured fallback model, or ``None`` for the cockpit default
    (the newest model of the next tier down — see ``_teammate_tier``)."""
    return _load_fallbacks().get(provider)


def set_fallback(provider: str, model: str) -> None:
    """Set (or, with a blank value, clear) a provider's fallback model."""
    if provider not in _providers():
        raise ValueError(f"unknown provider: {provider!r}")
    from .core.storage.v2_target import write_data

    fallbacks = _load_fallbacks()
    normalized = model.strip()
    if normalized:
        fallbacks[provider] = normalized
    else:
        fallbacks.pop(provider, None)
    write_data(fallback_path(), fallbacks)
