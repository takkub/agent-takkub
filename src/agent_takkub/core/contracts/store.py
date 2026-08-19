"""Storage contracts (Phase 8 target — split config/state/runtime/cache).
`core/storage/jsonl_store.py` is Phase 1's concrete `StateStore`-shaped
implementation; these Protocols are the seam future stores (SQLite, remote)
would satisfy the same way.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConfigStore(Protocol):
    def load(self, key: str) -> Any: ...

    def save(self, key: str, value: Any) -> None: ...


@runtime_checkable
class StateStore(Protocol):
    def append(self, record: dict[str, Any]) -> None: ...

    def read_all(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class RuntimeStore(Protocol):
    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...
