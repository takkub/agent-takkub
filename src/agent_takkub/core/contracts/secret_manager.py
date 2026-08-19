"""Contract for the Secret Manager (Phase 3 — pulled forward ahead of
Version/Migration per the plan §2, because Phase 2's `ProviderAccount.secret_ref`
needs somewhere to resolve to before it ships)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretManager(Protocol):
    def get_secret(self, secret_ref: str) -> str: ...

    def set_secret(self, secret_ref: str, value: str) -> None: ...

    def delete_secret(self, secret_ref: str) -> None: ...
