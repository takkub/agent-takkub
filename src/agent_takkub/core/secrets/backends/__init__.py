"""Shared vocabulary for secret backends: a fixed set of primitives
(get/set/delete + a doctor-facing status) every concrete backend implements
the same way, without a common base class — mirrors how `core/storage/
jsonl_store.py`'s `JsonlStore` satisfies `core.contracts.store.StateStore`
structurally rather than by inheritance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class BackendStatus(StrEnum):
    FOUND = "found"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class SecretUnavailableError(RuntimeError):
    """Raised when a secret_ref names a provider/backend this platform/build
    cannot serve. Never swallowed silently — a caller that explicitly asked
    for a secret must find out its request cannot be served, not get back a
    falsy value indistinguishable from "not logged in yet"."""


@runtime_checkable
class SecretBackend(Protocol):
    name: str

    def status(self, account_id: str) -> BackendStatus: ...

    def get(self, account_id: str) -> str | None: ...

    def set(self, account_id: str, value: str) -> None: ...

    def delete(self, account_id: str) -> None: ...
