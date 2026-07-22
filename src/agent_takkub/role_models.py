"""Persist the optional model selected for each *role*, bound to the provider
it was chosen for.

State file: ``~/.takkub/role-models.json`` — ``{role: {"provider": p, "model": m}}``.

**Why the provider is stored with the model:** a model id is only meaningful to
the CLI it was picked for (`k2.5` means nothing to codex, `opus` means nothing
to kimi), while a role's provider can change out from under the stored value in
three ways — the user re-points the role at another CLI, a different project
maps the same role name to a different CLI, or the chosen provider is
disabled/not installed so ``effective_provider_for`` substitutes claude. Keying
the model by role alone would then pass e.g. ``--model k2.5`` to a claude
substitute pane and break it. :func:`model_for` therefore takes the provider
that is actually about to spawn and returns the stored model only when it
matches; any other case falls back to the provider-level model
(:mod:`provider_models`) and then the CLI's own default.

A role with no entry (or an empty value) falls back the same way. Missing or
corrupt state behaves as empty.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS_HOME

_PATH = SETTINGS_HOME / "role-models.json"


def path() -> Path:
    """Where role-model selections live. Function form lets tests patch ``_PATH``."""
    return _PATH


def _load() -> dict[str, dict[str, str]]:
    """Load and sanitize configured role models; invalid state behaves as empty.

    Tolerates a legacy bare-string value (``{role: "model"}``) by dropping it —
    such an entry carries no provider, so honouring it is exactly the
    wrong-model-to-wrong-CLI hazard this module exists to prevent.
    """
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        role = str(key).strip()
        if not role or not isinstance(value, dict):
            continue
        provider = str(value.get("provider", "")).strip()
        model = value.get("model", "")
        model = model.strip() if isinstance(model, str) else ""
        if provider and model:
            cleaned[role] = {"provider": provider, "model": model}
    return cleaned


def _save(entries: dict[str, dict[str, str]]) -> None:
    """Persist role-model selections atomically, dropping incomplete entries."""
    cleaned = {
        role: {"provider": entry["provider"], "model": entry["model"]}
        for role, entry in entries.items()
        if entry.get("provider") and entry.get("model")
    }
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(_PATH)


def model_for(role: str, provider: str) -> str | None:
    """Model configured for *role* **when it spawns on *provider***, else None.

    *provider* must be the provider actually about to run (i.e. the result of
    ``effective_provider_for``), so a substituted or re-pointed role never
    inherits a model meant for a different CLI.
    """
    entry = _load().get(role)
    if entry is None or not provider:
        return None
    return entry["model"] if entry["provider"] == provider else None


def raw_model_for(role: str) -> tuple[str, str] | None:
    """``(provider, model)`` stored for *role*, ignoring which CLI will spawn.

    For UI/reporting only — never for building a spawn argv (see
    :func:`model_for`)."""
    entry = _load().get(role)
    return (entry["provider"], entry["model"]) if entry else None


def set_model(role: str, provider: str, model: str) -> None:
    """Bind *model* to (*role*, *provider*). Empty model clears the entry."""
    role = role.strip()
    if not role:
        raise ValueError("role must be a non-empty string")
    provider = (provider or "").strip()
    normalized = (model or "").strip()
    if not normalized:
        clear_model(role)
        return
    if not provider:
        raise ValueError("provider must be a non-empty string when setting a model")
    entries = _load()
    entries[role] = {"provider": provider, "model": normalized}
    _save(entries)


def clear_model(role: str) -> None:
    """Clear a role model so it falls back to the provider/CLI default."""
    entries = _load()
    entries.pop(role.strip(), None)
    _save(entries)


def all_models() -> dict[str, dict[str, str]]:
    """All configured role models as ``{role: {"provider", "model"}}``."""
    return _load()
