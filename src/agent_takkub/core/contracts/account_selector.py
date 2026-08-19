"""Contract for picking one account out of a pool (Phase 2 — priority/
sticky/quota-aware strategies, `AccountPool.strategy`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agent_takkub.core.models.account import AccountPool, ProviderAccount


@runtime_checkable
class AccountSelector(Protocol):
    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None: ...
