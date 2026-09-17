"""Persist the optional provider/model/reasoning-effort selected for each
*role*.

State file: ``v2/models/aliases.json`` under the cockpit's data home (#504
cut half — this module reads/writes that V2 target directly, no V1 file,
no dual-write mirror) —
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

**Per-project scoping (#657):** ``v2/models/aliases-projects.json`` holds
``{project: {role: entry}}`` — same entry shape as the global file. The
resolution contract deliberately MIRRORS ``provider_config.load_providers``:
a *project* with a bucket (even an empty one) resolves wholly from that
bucket; a project with no bucket falls back to the global entries. The two
stay consistent because the Settings Roles page writes both on the same
Save click (providers into ``routing.json``'s ``projects`` bucket via
``save_role_overrides``, model/effort in here), and the first project-scoped
write seeds the bucket from a snapshot of the global entries — the same
first-save snapshot ``save_role_overrides`` produces for providers, so a
project's first save captures what the page was showing, not an empty slate.
``set_provider`` stays global-only on purpose: a project's role→provider
truth lives in ``routing.json``'s ``projects`` bucket, not here.
"""

from __future__ import annotations

from pathlib import Path

from .provider_spec import PROVIDER_REGISTRY


def _target() -> Path:
    from .core.storage.layout import storage_layout_v2
    from .core.storage.v2_target import effective_data_home

    home = effective_data_home(None, prefer_primary=True)
    return storage_layout_v2(home).models / "aliases.json"


def path() -> Path:
    """Where role-model selections live (the V2 target — #504 cut half).
    Function form lets tests patch it."""
    return _target()


def projects_path() -> Path:
    """Where per-project role-model buckets live (#657) — sibling of
    :func:`path` in the same V2 models folder. Function form lets tests
    patch it, same as `path`."""
    return _target().with_name("aliases-projects.json")


def _load() -> dict[str, dict[str, str]]:
    """Load and sanitize configured role models; invalid state behaves as empty.

    Tolerates a legacy bare-string value (``{role: "model"}``) by dropping it —
    such an entry carries no provider, so honouring it is exactly the
    wrong-model-to-wrong-CLI hazard this module exists to prevent.
    """
    from .core.storage.v2_target import read_data

    data = read_data(path())
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


def _load_projects() -> dict[str, dict[str, dict[str, str]]]:
    """``{project: {role: entry}}`` buckets (#657); invalid state behaves as
    empty, each bucket sanitized exactly like the global file."""
    from .core.storage.v2_target import read_data

    data = read_data(projects_path())
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, dict[str, str]]] = {}
    for key, bucket in data.items():
        project = str(key).strip()
        if not project or not isinstance(bucket, dict):
            continue
        # An empty bucket is meaningful ("this project saved all-defaults"),
        # same as routing.json's `projects` contract — keep it.
        out[project] = _sanitize(bucket)
    return out


def _save_projects(buckets: dict[str, dict[str, dict[str, str]]]) -> None:
    """Persist the per-project buckets atomically. Unlike :func:`_save` this
    never mirrors anything into ``routing.json`` — the project provider
    bucket there is owned by ``provider_config.save_role_overrides``, which
    the Settings Roles page calls on the same Save click that lands here."""
    from .core.storage.v2_target import write_data

    cleaned: dict[str, dict[str, dict[str, str]]] = {}
    for project, bucket in buckets.items():
        clean_bucket: dict[str, dict[str, str]] = {}
        for role, entry in bucket.items():
            provider = entry.get("provider")
            if not provider:
                continue
            clean_entry = {"provider": provider}
            if entry.get("model"):
                clean_entry["model"] = entry["model"]
            if entry.get("effort"):
                clean_entry["effort"] = entry["effort"]
            clean_bucket[role] = clean_entry
        cleaned[project] = clean_bucket
    write_data(projects_path(), cleaned)


def _entries_for(project: str | None) -> dict[str, dict[str, str]]:
    """The entry set *project* resolves against (#657): its own bucket when
    one exists (even empty — mirrors ``provider_config.load_providers``'s
    "a project WITH an entry resolves from it, full stop"), else the global
    entries."""
    if project:
        buckets = _load_projects()
        if project in buckets:
            return buckets[project]
    return _load()


def _save(entries: dict[str, dict[str, str]]) -> None:
    """Persist role-model selections atomically (V2 target only, #504 cut
    half), dropping entries that carry neither a model nor an effort
    override.

    Also refreshes `config/routing.json`'s `global` bucket (B-H2,
    2026-09-07 round-2 review) — every setter in this module funnels
    through here, including the model picker's direct calls that never go
    through `provider_config.save_providers`, which used to be the only
    caller that kept that bucket current. The `projects` bucket is read
    back and passed through unchanged — this call never recomputes it, so
    it can never disagree with whatever `provider_config.save_providers`
    last wrote there for a given project."""
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

    from .core.storage.v2_target import write_data

    write_data(path(), cleaned)

    from . import provider_config

    routing = provider_config._read_routing()
    provider_config._write_routing(
        {role: entry["provider"] for role, entry in cleaned.items()}, routing["projects"]
    )


def model_for(role: str, provider: str, project: str | None = None) -> str | None:
    """Model configured for *role* **when it spawns on *provider***, else None.

    *provider* must be the provider actually about to run (i.e. the result of
    ``effective_provider_for``), so a substituted or re-pointed role never
    inherits a model meant for a different CLI. *project* selects that
    project's bucket when it has one (#657 — see module docstring).
    """
    entry = _entries_for(project).get(role)
    if entry is None or not provider or entry.get("provider") != provider:
        return None
    return entry.get("model") or None


def effort_for(role: str, provider: str, project: str | None = None) -> str | None:
    """Effort level configured for *role* **when it spawns on *provider***,
    else None. Mirrors :func:`model_for`'s provider-matching contract — see
    its docstring for why the provider must be the one actually about to
    spawn (and for *project*'s bucket semantics)."""
    entry = _entries_for(project).get(role)
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


def raw_model_for(role: str, project: str | None = None) -> tuple[str, str] | None:
    """``(provider, model)`` stored for *role*, ignoring which CLI will spawn.

    For UI/reporting only — never for building a spawn argv (see
    :func:`model_for`)."""
    entry = _entries_for(project).get(role)
    return (entry["provider"], entry.get("model", "")) if entry else None


def _merge_field(
    entries: dict[str, dict[str, str]], role: str, provider: str, field: str, value: str
) -> None:
    """Apply one (*role*, *provider*, *field*) change to *entries* in place —
    the shared read-modify-write core of :func:`_set_field` for both the
    global entries and a project bucket (#657).

    Preserves the entry's other field (model when setting effort, effort
    when setting model) as long as the provider matches what's already
    stored — a provider switch drops it, same wrong-CLI hazard the module
    docstring explains for model/effort individually. An empty *value*
    clears just this field; the role's entry is dropped entirely once
    neither field remains.
    """
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


def _set_field(
    role: str, provider: str, field: str, value: str, project: str | None = None
) -> None:
    """Read-modify-write one field of (*role*, *provider*)'s stored entry —
    in *project*'s bucket when given, else the global entries. A project's
    FIRST scoped write seeds its bucket from a snapshot of the global
    entries (see module docstring for why that mirrors
    ``save_role_overrides``'s provider behavior)."""
    role = role.strip()
    if not role:
        raise ValueError("role must be a non-empty string")
    provider = (provider or "").strip()
    value = (value or "").strip()
    if project:
        buckets = _load_projects()
        bucket = buckets.get(project)
        if bucket is None:
            bucket = {r: dict(e) for r, e in _load().items()}
        _merge_field(bucket, role, provider, field, value)
        buckets[project] = bucket
        _save_projects(buckets)
        return
    entries = _load()
    _merge_field(entries, role, provider, field, value)
    _save(entries)


def set_model(role: str, provider: str, model: str, project: str | None = None) -> None:
    """Bind *model* to (*role*, *provider*) — in *project*'s bucket when
    given (#657). Empty model clears the model override while preserving any
    effort override already stored for the same provider."""
    _set_field(role, provider, "model", model, project)


def set_effort(role: str, provider: str, effort: str, project: str | None = None) -> None:
    """Bind *effort* to (*role*, *provider*) — in *project*'s bucket when
    given (#657). Empty effort clears the effort override while preserving
    any model override already stored for the same provider."""
    _set_field(role, provider, "effort", effort, project)


def clear_model(role: str, project: str | None = None) -> None:
    """Clear a role's stored entry entirely (model and effort — both live
    under the same per-provider entry) so it falls back to defaults. With
    *project*: clears only that project's bucket entry (the bucket itself
    stays — the project remains self-scoped, same as routing.json)."""
    if project:
        buckets = _load_projects()
        bucket = buckets.get(project)
        if bucket is not None:
            bucket.pop(role.strip(), None)
            _save_projects(buckets)
        return
    entries = _load()
    entries.pop(role.strip(), None)
    _save(entries)


def all_models(project: str | None = None) -> dict[str, dict[str, str]]:
    """All configured role overrides as ``{role: {"provider", "model"?,
    "effort"?}}`` — *project*'s bucket when it has one, else global."""
    return _entries_for(project)


def project_buckets() -> dict[str, dict[str, dict[str, str]]]:
    """Every per-project bucket as ``{project: {role: entry}}`` (#657) — for
    maintenance sweeps that must see every stored pin, like
    `provider_model_refresh`'s stale-pin bump."""
    return _load_projects()
