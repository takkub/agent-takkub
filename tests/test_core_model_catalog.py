"""core.model_catalog — registry store + legacy reader
(docs/v2/2.0.0-migration-plan.md §1.3, epic #309)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.core.model_catalog.legacy import (
    read_legacy_model_profiles,
    read_legacy_models,
    read_legacy_provider_model_pin,
    read_legacy_role_model_pin,
)
from agent_takkub.core.model_catalog.registry import ModelProfileRegistry, ModelRegistry
from agent_takkub.core.models.model import ModelDefinition, ModelProfile
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.layout import storage_layout_v2


def _model(id_, provider="claude", name="sonnet"):
    return ModelDefinition(id=id_, provider_id=provider, name=name)


def _profile(id_, model_id="m1"):
    return ModelProfile(id=id_, name=id_, model_id=model_id)


# ── ModelRegistry ─────────────────────────────────────────────────────────


def test_model_registry_upsert_and_get(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    reg.upsert(_model("m1"))
    got = reg.get("m1")
    assert got is not None
    assert got.provider_id == "claude"
    assert got.name == "sonnet"


def test_model_registry_upsert_is_latest_wins(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    reg.upsert(_model("m1", name="sonnet"))
    reg.upsert(_model("m1", name="opus"))
    models = reg.all()
    assert len(models) == 1
    assert models[0].name == "opus"


def test_model_registry_delete_is_tombstone(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    reg.upsert(_model("m1"))
    reg.delete("m1")
    assert reg.get("m1") is None
    assert reg.all() == []


def test_model_registry_get_missing_returns_none(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    assert reg.get("nope") is None


def test_model_registry_for_provider_filters(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    reg.upsert(_model("m1", provider="claude"))
    reg.upsert(_model("m2", provider="codex"))
    assert [m.id for m in reg.for_provider("codex")] == ["m2"]


def test_model_registry_empty_store_reads_empty(tmp_path):
    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    assert reg.all() == []


# ── ModelProfileRegistry ──────────────────────────────────────────────────


def test_profile_registry_round_trip(tmp_path):
    reg = ModelProfileRegistry(JsonlStore(tmp_path / "profiles.jsonl"))
    reg.upsert(_profile("backend", model_id="m1"))
    profile = reg.get("backend")
    assert profile is not None
    assert profile.model_id == "m1"


def test_profile_registry_upsert_is_latest_wins(tmp_path):
    reg = ModelProfileRegistry(JsonlStore(tmp_path / "profiles.jsonl"))
    reg.upsert(_profile("backend", model_id="m1"))
    reg.upsert(_profile("backend", model_id="m2"))
    profiles = reg.all()
    assert len(profiles) == 1
    assert profiles[0].model_id == "m2"


def test_profile_registry_delete_is_tombstone(tmp_path):
    reg = ModelProfileRegistry(JsonlStore(tmp_path / "profiles.jsonl"))
    reg.upsert(_profile("backend"))
    reg.delete("backend")
    assert reg.get("backend") is None
    assert reg.all() == []


def test_profile_registry_for_model_filters(tmp_path):
    reg = ModelProfileRegistry(JsonlStore(tmp_path / "profiles.jsonl"))
    reg.upsert(_profile("backend", model_id="m1"))
    reg.upsert(_profile("frontend", model_id="m2"))
    assert [p.id for p in reg.for_model("m2")] == ["frontend"]


# ── legacy reader ─────────────────────────────────────────────────────────


def _write_wrapped(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": 1, "migrated_from": "x", "migrated_at": 0.0, "data": data}),
        encoding="utf-8",
    )


def test_read_legacy_models_missing_file_returns_empty(tmp_path):
    assert read_legacy_models(tmp_path) == []


def test_read_legacy_models_corrupt_json_returns_empty(tmp_path):
    layout = storage_layout_v2(tmp_path)
    layout.models.mkdir(parents=True)
    (layout.models / "registry.json").write_text("{not json", encoding="utf-8")
    assert read_legacy_models(tmp_path) == []


def test_read_legacy_models_empty_data_returns_empty(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "registry.json", {})
    assert read_legacy_models(tmp_path) == []


def test_read_legacy_models_parses_wrapped_blob(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "registry.json", {"claude": "claude-sonnet-5", "codex": "gpt-5"})
    models = read_legacy_models(tmp_path)
    by_provider = {m.provider_id: m for m in models}
    assert by_provider["claude"].id == "claude:claude-sonnet-5"
    assert by_provider["claude"].name == "claude-sonnet-5"
    assert by_provider["codex"].id == "codex:gpt-5"


def test_read_legacy_models_same_model_two_providers_survives_registry_upsert(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(
        layout.models / "registry.json",
        {"claude": "claude-sonnet-5", "cursor": "claude-sonnet-5", "codex": "gpt-5"},
    )
    models = read_legacy_models(tmp_path)
    assert len(models) == 3
    ids = {m.id for m in models}
    assert ids == {"claude:claude-sonnet-5", "cursor:claude-sonnet-5", "codex:gpt-5"}

    reg = ModelRegistry(JsonlStore(tmp_path / "models.jsonl"))
    for model in models:
        reg.upsert(model)

    all_models = reg.all()
    assert len(all_models) == 3
    assert [m.name for m in reg.for_provider("claude")] == ["claude-sonnet-5"]
    assert [m.name for m in reg.for_provider("cursor")] == ["claude-sonnet-5"]
    assert [m.name for m in reg.for_provider("codex")] == ["gpt-5"]


def test_read_legacy_model_profiles_missing_file_returns_empty(tmp_path):
    assert read_legacy_model_profiles(tmp_path) == []


def test_read_legacy_model_profiles_corrupt_json_returns_empty(tmp_path):
    layout = storage_layout_v2(tmp_path)
    layout.models.mkdir(parents=True)
    (layout.models / "aliases.json").write_text("{not json", encoding="utf-8")
    assert read_legacy_model_profiles(tmp_path) == []


def test_read_legacy_model_profiles_empty_data_returns_empty(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "aliases.json", {})
    assert read_legacy_model_profiles(tmp_path) == []


def test_read_legacy_model_profiles_parses_wrapped_blob(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(
        layout.models / "aliases.json",
        {
            "backend": {"provider": "claude", "model": "claude-sonnet-5", "effort": "high"},
            "frontend": {"provider": "codex", "effort": "high"},
        },
    )
    profiles = read_legacy_model_profiles(tmp_path)
    assert len(profiles) == 1
    assert profiles[0].id == "backend"
    assert profiles[0].model_id == "claude-sonnet-5"


# ── read_legacy_role_model_pin / read_legacy_provider_model_pin ────────────
# (epic #309 Wave C, plan §1.3) — unlike read_legacy_model_profiles() above,
# these keep the `provider` a pin was saved for, which core.routing.Router
# needs to reproduce V1 role_models.model_for()'s provider-gating exactly.


def test_read_legacy_role_model_pin_missing_file_returns_none(tmp_path):
    assert read_legacy_role_model_pin("backend", tmp_path) is None


def test_read_legacy_role_model_pin_corrupt_json_returns_none(tmp_path):
    layout = storage_layout_v2(tmp_path)
    layout.models.mkdir(parents=True)
    (layout.models / "aliases.json").write_text("{not json", encoding="utf-8")
    assert read_legacy_role_model_pin("backend", tmp_path) is None


def test_read_legacy_role_model_pin_role_not_present_returns_none(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(
        layout.models / "aliases.json", {"frontend": {"provider": "codex", "model": "m1"}}
    )
    assert read_legacy_role_model_pin("backend", tmp_path) is None


def test_read_legacy_role_model_pin_effort_only_entry_returns_none(tmp_path):
    """No `model` in the entry (effort-only) is nothing a model pin can
    represent — same "skip, don't guess" rule read_legacy_model_profiles()
    already documents."""
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(
        layout.models / "aliases.json", {"backend": {"provider": "claude", "effort": "high"}}
    )
    assert read_legacy_role_model_pin("backend", tmp_path) is None


def test_read_legacy_role_model_pin_parses_provider_and_model(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(
        layout.models / "aliases.json",
        {"backend": {"provider": "claude", "model": "claude-opus-5", "effort": "high"}},
    )
    assert read_legacy_role_model_pin("backend", tmp_path) == ("claude", "claude-opus-5")


def test_read_legacy_provider_model_pin_missing_file_returns_none(tmp_path):
    assert read_legacy_provider_model_pin("claude", tmp_path) is None


def test_read_legacy_provider_model_pin_corrupt_json_returns_none(tmp_path):
    layout = storage_layout_v2(tmp_path)
    layout.models.mkdir(parents=True)
    (layout.models / "registry.json").write_text("{not json", encoding="utf-8")
    assert read_legacy_provider_model_pin("claude", tmp_path) is None


def test_read_legacy_provider_model_pin_provider_not_present_returns_none(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "registry.json", {"codex": "gpt-5.6"})
    assert read_legacy_provider_model_pin("claude", tmp_path) is None


def test_read_legacy_provider_model_pin_unregistered_provider_returns_none(tmp_path):
    """V1 `provider_models._load()` silently drops any key not in
    `provider_spec.PROVIDER_REGISTRY` — a stale migrated entry for a
    provider since removed from the registry must not resurrect a pin V1
    itself would no longer honour (epic #309 Wave C parity)."""
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "registry.json", {"some-retired-provider": "old-model"})
    assert read_legacy_provider_model_pin("some-retired-provider", tmp_path) is None


def test_read_legacy_provider_model_pin_parses_model(tmp_path):
    layout = storage_layout_v2(tmp_path)
    _write_wrapped(layout.models / "registry.json", {"claude": "claude-sonnet-5"})
    assert read_legacy_provider_model_pin("claude", tmp_path) == "claude-sonnet-5"
