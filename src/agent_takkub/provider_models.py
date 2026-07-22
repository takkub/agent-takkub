"""Persist the optional CLI model selected for each provider.

State file: ``~/.takkub/provider-models.json``.  Missing, corrupt, or stale
entries are treated as absent so each provider falls back to its own CLI
default unless the user explicitly selects a model.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS_HOME

_PATH = SETTINGS_HOME / "provider-models.json"


def path() -> Path:
    """Where model selections live. Function form lets tests patch ``_PATH``."""
    return _PATH


def _providers() -> frozenset[str]:
    """Return registered provider names without creating an import cycle."""
    from .provider_spec import PROVIDER_REGISTRY

    return frozenset(PROVIDER_REGISTRY)


def _load() -> dict[str, str]:
    """Load and sanitize configured models; invalid state behaves as empty."""
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
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
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)


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


def all_models() -> dict[str, str]:
    """Return all configured provider models as a fresh mapping."""
    return _load()
