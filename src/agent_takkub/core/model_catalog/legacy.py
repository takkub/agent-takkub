"""Legacy reader: ladder-step-1's wrapped `models/registry.json` +
`models/aliases.json` (`RegistryCopyStep`/`build_readonly_registries_step`,
migration plan §1.3) -> real `ModelDefinition`/`ModelProfile` objects.

Lives here rather than `core.storage.legacy_reader` for the same reason
`core.accounts.legacy_reader` lives in `core.accounts` and not
`core.storage`: a reader that builds one domain's objects belongs next to
that domain's registry, not in the shared storage-utilities module (which
only hosts the generic fail-open JSON framework `read_json` plus the one
cross-domain reader that predates any domain package existing). Nothing
forbids it either way — the `core-models-pure` import-linter contract only
restricts `agent_takkub.core.models.*` (the pure vocabulary dataclasses),
not this package — so this is a placement choice, not a contract requirement.

Ladder step 1 (`RegistryCopyStep`) never reshapes V1 JSON — it wraps it
verbatim (`{"schema", "migrated_from", "migrated_at", "data": <v1 json>}`).
Both readers below unwrap `.data` and convert it, fail-open throughout
(missing target file / invalid JSON / a `.data` that isn't the expected
shape all read as an empty list, never raise) — this is the normal case on
every machine today since nothing has run `migrate apply` yet (plan §1.3's
own dependency note: the ladder must actually run before these targets
exist with real data).
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub.core.models.model import ModelDefinition, ModelProfile
from agent_takkub.core.storage.layout import storage_layout_v2
from agent_takkub.core.storage.legacy_reader import read_json


def read_legacy_models(data_home: Path | None = None) -> list[ModelDefinition]:
    """V1 `provider-models.json` (`{provider: model_id}`) wrapped by ladder
    step 1 into `models/registry.json` -> one `ModelDefinition` per entry.

    V1 only ever stored a model id string per provider, never a name or
    context window, so `name` is derived directly as the model id string
    and everything else stays at `ModelDefinition`'s default — no per-model
    metadata table is guessed here.

    `id` is `f"{provider_id}:{model_id}"`, NOT the bare model id. Two
    providers can legitimately point at the same underlying model (e.g.
    `claude` and `cursor` both set to `claude-sonnet-5`), and
    `ModelRegistry` is an upsert-log keyed by `id` — latest record per `id`
    wins (mirrors `core.accounts`, where `ProviderAccount.id` is already
    unique system-wide). A bare model id would let the second provider's
    upsert silently clobber the first in the log, so `registry.all()` and
    `for_provider()` would both lose the earlier entry with no error. Do
    not "simplify" this back to the bare model id — `name` already carries
    the bare id for display.
    """
    path = storage_layout_v2(data_home).models / "registry.json"
    data = read_json(path).get("data")
    if not isinstance(data, dict):
        return []
    models: list[ModelDefinition] = []
    for provider_id, model_id in data.items():
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        clean_provider_id = str(provider_id)
        clean_model_id = model_id.strip()
        models.append(
            ModelDefinition(
                id=f"{clean_provider_id}:{clean_model_id}",
                provider_id=clean_provider_id,
                name=clean_model_id,
            )
        )
    return models


def read_legacy_model_profiles(data_home: Path | None = None) -> list[ModelProfile]:
    """V1 `role-models.json` (`{role: {"provider":.., "model":.., "effort":..}}`)
    wrapped by ladder step 1 into `models/aliases.json` -> one `ModelProfile`
    per role entry that carries a `model` override.

    `ModelProfile` has no `provider`/`effort` fields (see `core/models/model.py`),
    so BOTH of these are lost on every entry, not only the effort-only case:
    - a role entry that only overrides `effort` (no `model`) is skipped
      entirely — `model_id` has no default to fall back to, so there is
      nothing a `ModelProfile` can represent, and it is skipped rather than
      guessed.
    - a role entry that DOES carry a `model` override still drops its
      `provider`/`effort` values silently — only `model_id` survives into
      the `ModelProfile`.
    This data loss is a known open design question, not an oversight here:
    `docs/v2/2.0.0-migration-plan.md` §1.3 already flags the related
    "account-scoped model override" question as unresolved
    (`REUSE_VS_REWRITE_MATRIX.md:46`) and defers resolver/schema work to
    Wave C. Whether `ModelProfile` should grow `provider`/`effort` fields to
    stop dropping this data is a Wave C decision, not something to guess at
    in this fail-open legacy reader.
    """
    path = storage_layout_v2(data_home).models / "aliases.json"
    data = read_json(path).get("data")
    if not isinstance(data, dict):
        return []
    profiles: list[ModelProfile] = []
    for role, entry in data.items():
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("model")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        role_name = str(role).strip()
        if not role_name:
            continue
        profiles.append(ModelProfile(id=role_name, name=role_name, model_id=model_id.strip()))
    return profiles
