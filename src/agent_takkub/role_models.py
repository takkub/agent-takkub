"""Persist the optional provider/model/reasoning-effort selected for each
*role*.

State file: ``~/.takkub/role-models.json`` —
``{role: {"provider": p, "model": m, "effort": e}}`` (``model``/``effort``
both optional — a role's entry only needs to carry whichever axis the user
actually overrode; a bare ``{"provider": p}`` entry — no model, no effort —
is a **plain role→provider override** with nothing else pinned, same thing
`provider_config.py`'s now-retired standalone ``role-providers.json`` used
to store (#515 Settings diet folded that file in here — see
`provider_for_role`/`set_provider` below — "one file, one source of truth"
instead of two files that could disagree about the same role's provider).
An entry is only dropped once it carries neither a provider nor a model nor
an effort.

**Why the provider is stored with the model/effort:** a model id or effort
level is only meaningful to the CLI it was picked for (`k2.5` means nothing
to codex, `--effort high` means nothing to a provider with no effort_flag),
while a role's provider can change out from under the stored value in three
ways — the user re-points the role at another CLI, a different project maps
the same role name to a different CLI, or the chosen provider is
disabled/not installed so ``effective_provider_for`` substitutes claude.
Keying a value by role alone would then pass e.g. ``--model k2.5`` to a
claude substitute pane and break it. :func:`model_for` / :func:`effort_for`
therefore take the provider that is actually about to spawn and return the
stored value only when it matches; any other case falls back the same way
as an unset role (provider-level default, then the CLI's own default).

A role with no entry (or an empty value) falls back the same way. Missing or
corrupt state behaves as empty. An ``effort`` value not in the stored
provider's :data:`~.provider_spec.ProviderSpec.effort_levels` is dropped at
load time — only that field, not the whole entry — same as any other
sanitizer here.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS_HOME
from .provider_spec import PROVIDER_REGISTRY

_PATH = SETTINGS_HOME / "role-models.json"


def path() -> Path:
    """Where role-model selections live. Function form lets tests patch ``_PATH``."""
    return _PATH


def _load() -> dict[str, dict[str, str]]:
    """Load and sanitize configured role models; invalid state behaves as empty.

    Tolerates a legacy bare-string value (``{role: "model"}``) by dropping it —
    such an entry carries no provider, so honouring it is exactly the
    wrong-model-to-wrong-CLI hazard this module exists to prevent.

    ``TAKKUB_V2_AUTHORITY`` (#362 Phase 10 wave 2, default off): when on and
    the dual-written ``v2/`` mirror exists, sanitizes THAT instead of the V1
    file — same sanitizer either way, so callers see identical output. Falls
    back to V1 on any v2 miss (not migrated / corrupt mirror).
    """
    from .core.storage.v2_authority import read_role_models, v2_authority_enabled

    if v2_authority_enabled():
        v2_data = read_role_models()
        if isinstance(v2_data, dict):
            return _sanitize(v2_data)

    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _sanitize(data)


def _sanitize(data: dict) -> dict[str, dict[str, str]]:
    cleaned: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        role = str(key).strip()
        if not role or not isinstance(value, dict):
            continue
        provider = str(value.get("provider", "")).strip()
        model = value.get("model", "")
        model = model.strip() if isinstance(model, str) else ""
        effort = value.get("effort", "")
        effort = effort.strip() if isinstance(effort, str) else ""
        if not provider:
            continue
        entry: dict[str, str] = {"provider": provider}
        if model:
            entry["model"] = model
        if effort:
            spec = PROVIDER_REGISTRY.get(provider)
            if spec is None or effort in spec.effort_levels:
                entry["effort"] = effort
            # else: not a level this provider's CLI accepts — drop only this
            # field, the (provider, model) pair underneath is still meaningful.
        cleaned[role] = entry
    return cleaned


def _save(entries: dict[str, dict[str, str]]) -> None:
    """Persist role-model selections atomically, dropping entries that carry
    neither a model nor an effort override."""
    cleaned: dict[str, dict[str, str]] = {}
    for role, entry in entries.items():
        provider = entry.get("provider")
        model = entry.get("model")
        effort = entry.get("effort")
        if not provider:
            continue
        clean_entry = {"provider": provider}
        if model:
            clean_entry["model"] = model
        if effort:
            clean_entry["effort"] = effort
        cleaned[role] = clean_entry
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(_PATH)

    from . import config as _config
    from . import provider_config
    from .core.storage.dual_write import dual_write_role_models, dual_write_routing
    from .core.storage.legacy_reader import read_json

    dual_write_role_models(cleaned)
    # #515 folded the standalone global-routing file into this one — this
    # is now the only global-routing writer, so it must mirror `routing.json`
    # itself instead of relying on `provider_config.save_providers` (which
    # this call never goes through — see B-H2 in
    # docs/audit/2026-09-07-batch-2.0.x-review-round2.md). The global half
    # comes straight from `cleaned` (this save's own in-memory result), not
    # a re-read, for the same reason `provider_config.save_providers`
    # passes `load_providers(None)` rather than trusting some other source.
    dual_write_routing(
        {role: entry["provider"] for role, entry in cleaned.items()},
        {
            name: read_json(provider_config.config_path(name))
            for name in _config.list_project_names()
            if provider_config.config_path(name).exists()
        },
    )


def model_for(role: str, provider: str) -> str | None:
    """Model configured for *role* **when it spawns on *provider***, else None.

    *provider* must be the provider actually about to run (i.e. the result of
    ``effective_provider_for``), so a substituted or re-pointed role never
    inherits a model meant for a different CLI.
    """
    entry = _load().get(role)
    if entry is None or not provider or entry.get("provider") != provider:
        return None
    return entry.get("model") or None


def effort_for(role: str, provider: str) -> str | None:
    """Effort level configured for *role* **when it spawns on *provider***,
    else None. Mirrors :func:`model_for`'s provider-matching contract — see
    its docstring for why the provider must be the one actually about to
    spawn."""
    entry = _load().get(role)
    if entry is None or not provider or entry.get("provider") != provider:
        return None
    return entry.get("effort") or None


def set_provider(role: str, provider: str) -> None:
    """Persist a bare role→provider override with no model/effort pinned —
    clearing *provider* (falsy) drops the role's entire entry, same as
    `clear_model`. Switching to a different provider than what's already
    stored drops that entry's model/effort too (same wrong-CLI hazard
    `_set_field` guards against) rather than leaving a model pinned for a
    CLI it was never chosen for."""
    role = role.strip()
    if not role:
        raise ValueError("role must be a non-empty string")
    provider = (provider or "").strip()
    entries = _load()
    if not provider:
        entries.pop(role, None)
    else:
        existing = entries.get(role)
        entries[role] = dict(existing) if existing and existing.get("provider") == provider else {}
        entries[role]["provider"] = provider
    _save(entries)


def raw_model_for(role: str) -> tuple[str, str] | None:
    """``(provider, model)`` stored for *role*, ignoring which CLI will spawn.

    For UI/reporting only — never for building a spawn argv (see
    :func:`model_for`)."""
    entry = _load().get(role)
    return (entry["provider"], entry.get("model", "")) if entry else None


def _set_field(role: str, provider: str, field: str, value: str) -> None:
    """Read-modify-write one field of (*role*, *provider*)'s stored entry.

    Preserves the entry's other field (model when setting effort, effort
    when setting model) as long as the provider matches what's already
    stored — a provider switch drops it, same wrong-CLI hazard the module
    docstring explains for model/effort individually. An empty *value*
    clears just this field; the role's entry is dropped entirely once
    neither field remains.
    """
    role = role.strip()
    if not role:
        raise ValueError("role must be a non-empty string")
    provider = (provider or "").strip()
    value = (value or "").strip()
    entries = _load()
    existing = entries.get(role)
    merged = dict(existing) if existing and existing.get("provider") == provider else {}
    if value:
        if not provider:
            raise ValueError(f"provider must be a non-empty string when setting {field}")
        merged["provider"] = provider
        merged[field] = value
    else:
        merged.pop(field, None)
        if provider:
            merged["provider"] = provider
    if merged.get("provider") or merged.get("model") or merged.get("effort"):
        entries[role] = merged
    else:
        entries.pop(role, None)
    _save(entries)


def set_model(role: str, provider: str, model: str) -> None:
    """Bind *model* to (*role*, *provider*). Empty model clears the model
    override while preserving any effort override already stored for the
    same provider."""
    _set_field(role, provider, "model", model)


def set_effort(role: str, provider: str, effort: str) -> None:
    """Bind *effort* to (*role*, *provider*). Empty effort clears the effort
    override while preserving any model override already stored for the
    same provider."""
    _set_field(role, provider, "effort", effort)


def clear_model(role: str) -> None:
    """Clear a role's stored entry entirely (model and effort — both live
    under the same per-provider entry) so it falls back to defaults."""
    entries = _load()
    entries.pop(role.strip(), None)
    _save(entries)


def all_models() -> dict[str, dict[str, str]]:
    """All configured role overrides as ``{role: {"provider", "model"?, "effort"?}}``."""
    return _load()
