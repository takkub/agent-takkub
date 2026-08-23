"""`ModelRegistry`/`ModelProfileRegistry` — JSONL-backed model catalog store,
mirroring `core.accounts.registry`'s upsert-log/tombstone pattern (latest-
record-per-id wins, a `{"id": ..., "deleted": True}` record is a tombstone).

Lives in its own `core.model_catalog` package rather than `core.models.registry`
(the location `docs/v2/2.0.0-migration-plan.md` §1.3 originally named)
because the `core-models-pure` import-linter contract (pyproject.toml)
forbids `agent_takkub.core.models.*` from importing `agent_takkub.core.storage`
at all — "a model must never depend on how it gets persisted". A JSONL store
has to import `core.storage.jsonl_store` + `core.storage.paths`, so it cannot
live inside `core.models` without tripping that contract. This package plays
the same role for models that `core.accounts` already plays for accounts:
`core.models.model` stays pure vocabulary (`ModelDefinition`/`ModelProfile`,
untouched by this change), `core.model_catalog` is the storage layer on top
of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from agent_takkub.core.models.model import ModelDefinition, ModelProfile
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.paths import core_store_path


def _model_to_record(model: ModelDefinition) -> dict[str, Any]:
    return asdict(model)


def _record_to_model(record: Mapping[str, Any]) -> ModelDefinition:
    return ModelDefinition(
        id=record["id"],
        provider_id=record["provider_id"],
        name=record.get("name", ""),
        context_window=record.get("context_window"),
        supports_vision=bool(record.get("supports_vision", False)),
        supports_tools=bool(record.get("supports_tools", False)),
        user_id=record.get("user_id"),
        workspace_id=record.get("workspace_id"),
        project_id=record.get("project_id"),
    )


def _profile_to_record(profile: ModelProfile) -> dict[str, Any]:
    return asdict(profile)


def _record_to_profile(record: Mapping[str, Any]) -> ModelProfile:
    return ModelProfile(
        id=record["id"],
        name=record.get("name", ""),
        model_id=record["model_id"],
        description=record.get("description", ""),
        user_id=record.get("user_id"),
        workspace_id=record.get("workspace_id"),
        project_id=record.get("project_id"),
    )


class ModelRegistry:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path("models"))

    def upsert(self, model: ModelDefinition) -> None:
        self._store.append(_model_to_record(model))

    def delete(self, model_id: str) -> None:
        self._store.append({"id": model_id, "deleted": True})

    def all(self) -> list[ModelDefinition]:
        records, _ = self._store.read_all()
        latest: dict[str, Mapping[str, Any]] = {}
        for record in records:
            rid = record.get("id")
            if rid:
                latest[rid] = record
        return [_record_to_model(r) for r in latest.values() if not r.get("deleted")]

    def get(self, model_id: str) -> ModelDefinition | None:
        for model in self.all():
            if model.id == model_id:
                return model
        return None

    def for_provider(self, provider_id: str) -> list[ModelDefinition]:
        return [m for m in self.all() if m.provider_id == provider_id]


class ModelProfileRegistry:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path("model_profiles"))

    def upsert(self, profile: ModelProfile) -> None:
        self._store.append(_profile_to_record(profile))

    def delete(self, profile_id: str) -> None:
        self._store.append({"id": profile_id, "deleted": True})

    def all(self) -> list[ModelProfile]:
        records, _ = self._store.read_all()
        latest: dict[str, Mapping[str, Any]] = {}
        for record in records:
            rid = record.get("id")
            if rid:
                latest[rid] = record
        return [_record_to_profile(r) for r in latest.values() if not r.get("deleted")]

    def get(self, profile_id: str) -> ModelProfile | None:
        for profile in self.all():
            if profile.id == profile_id:
                return profile
        return None

    def for_model(self, model_id: str) -> list[ModelProfile]:
        return [p for p in self.all() if p.model_id == model_id]
