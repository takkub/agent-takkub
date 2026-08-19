"""`ProviderAccount`/`AccountPool` registries — JSONL-backed, latest-record-
per-id wins (an upsert log), a `{"id": ..., "deleted": True}` record is a
tombstone. Same append-only pattern `core.storage.jsonl_store` documents for
Phase 1 (REUSE_VS_REWRITE_MATRIX.md §2: "Account Pool / Selector — NEW").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from agent_takkub.core.models.account import (
    AccountPool,
    AccountStatus,
    ProviderAccount,
    SelectionStrategy,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.paths import core_store_path


def _account_to_record(account: ProviderAccount) -> dict[str, Any]:
    record = asdict(account)
    record["status"] = str(account.status)
    return record


def _record_to_account(record: Mapping[str, Any]) -> ProviderAccount:
    return ProviderAccount(
        id=record["id"],
        provider_id=record["provider_id"],
        label=record.get("label", ""),
        secret_ref=record.get("secret_ref"),
        status=AccountStatus(record.get("status", AccountStatus.ACTIVE)),
        priority=int(record.get("priority", 0)),
        user_id=record.get("user_id"),
        workspace_id=record.get("workspace_id"),
        project_id=record.get("project_id"),
    )


def _pool_to_record(pool: AccountPool) -> dict[str, Any]:
    record = asdict(pool)
    record["strategy"] = str(pool.strategy)
    record["account_ids"] = list(pool.account_ids)
    return record


def _record_to_pool(record: Mapping[str, Any]) -> AccountPool:
    return AccountPool(
        id=record["id"],
        name=record.get("name", ""),
        provider_id=record["provider_id"],
        account_ids=tuple(record.get("account_ids", ())),
        strategy=SelectionStrategy(record.get("strategy", SelectionStrategy.PRIORITY)),
        user_id=record.get("user_id"),
        workspace_id=record.get("workspace_id"),
        project_id=record.get("project_id"),
    )


class AccountRegistry:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path("accounts"))

    def upsert(self, account: ProviderAccount) -> None:
        self._store.append(_account_to_record(account))

    def delete(self, account_id: str) -> None:
        self._store.append({"id": account_id, "deleted": True})

    def all(self) -> list[ProviderAccount]:
        records, _ = self._store.read_all()
        latest: dict[str, Mapping[str, Any]] = {}
        for record in records:
            rid = record.get("id")
            if rid:
                latest[rid] = record
        return [_record_to_account(r) for r in latest.values() if not r.get("deleted")]

    def get(self, account_id: str) -> ProviderAccount | None:
        for account in self.all():
            if account.id == account_id:
                return account
        return None

    def for_provider(self, provider_id: str) -> list[ProviderAccount]:
        return [a for a in self.all() if a.provider_id == provider_id]


class AccountPoolRegistry:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path("account_pools"))

    def upsert(self, pool: AccountPool) -> None:
        self._store.append(_pool_to_record(pool))

    def delete(self, pool_id: str) -> None:
        self._store.append({"id": pool_id, "deleted": True})

    def all(self) -> list[AccountPool]:
        records, _ = self._store.read_all()
        latest: dict[str, Mapping[str, Any]] = {}
        for record in records:
            rid = record.get("id")
            if rid:
                latest[rid] = record
        return [_record_to_pool(r) for r in latest.values() if not r.get("deleted")]

    def get(self, pool_id: str) -> AccountPool | None:
        for pool in self.all():
            if pool.id == pool_id:
                return pool
        return None
