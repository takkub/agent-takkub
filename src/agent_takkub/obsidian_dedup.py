"""Persistent, cross-restart dedup index for Obsidian notes — issue #365
phase 8 improvement 2. A repeat write of the same fact (same
``knowledge_id``) must update the existing index record, never create a
second entry — the identical upsert-log shape
``core.accounts.registry.AccountRegistry`` already uses for the same
reason (append-only JSONL, latest record per id wins, a
``{"id": ..., "deleted": True}`` record tombstones).

Lives under ``core.storage.paths.core_home()`` (the "core_home JSONL"
option the phase-8 task names) — the same "resolves to V2's ``system/``
once migration creates it, ``RUNTIME_DIR/core`` until then" legacy
fallback every other Core V2 internal store already gets for free, with
no dependency on this machine having run the migration ladder.
"""

from __future__ import annotations

from typing import Any

from .core.storage.jsonl_store import JsonlStore
from .core.storage.paths import core_store_path


class DedupIndex:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path("obsidian_dedup"))

    def lookup(self, knowledge_id: str) -> dict[str, Any] | None:
        """Latest record for *knowledge_id*, or ``None`` if never recorded
        (or tombstoned)."""
        latest = self._latest_by_id().get(knowledge_id)
        if latest is None or latest.get("deleted"):
            return None
        return latest

    def record_write(
        self,
        knowledge_id: str,
        *,
        path: str,
        project_id: str,
        source: str,
        kind: str,
        content_hash: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Upsert the index entry for *knowledge_id* — appends one record;
        `lookup`/`all_records`/`count` only ever surface the latest per
        id, so a second call with the same id updates rather than
        duplicating."""
        self._store.append(
            {
                "id": knowledge_id,
                "path": path,
                "project_id": project_id,
                "source": source,
                "kind": kind,
                "content_hash": content_hash,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

    def _latest_by_id(self) -> dict[str, dict[str, Any]]:
        records, _ = self._store.read_all()
        latest: dict[str, dict[str, Any]] = {}
        for r in records:
            rid = r.get("id")
            if rid:
                latest[rid] = r
        return latest

    def all_records(self) -> list[dict[str, Any]]:
        """Latest, non-tombstoned record per ``knowledge_id``."""
        return [r for r in self._latest_by_id().values() if not r.get("deleted")]

    def count(self) -> int:
        return len(self.all_records())


# Module-level convenience wrappers construct a fresh `DedupIndex()` per
# call (never a cached singleton) — `core_store_path()` resolves through
# `config.RUNTIME_DIR` at call time, and caching it would freeze that
# resolution at first-import time instead, the same "late import is
# patchable" pitfall `tests/conftest.py`'s isolation fixture documents
# for `core.storage.paths` callers generally.


def lookup(knowledge_id: str) -> dict[str, Any] | None:
    return DedupIndex().lookup(knowledge_id)


def record_write(knowledge_id: str, **kwargs: Any) -> None:
    DedupIndex().record_write(knowledge_id, **kwargs)


def count() -> int:
    return DedupIndex().count()


def all_records() -> list[dict[str, Any]]:
    return DedupIndex().all_records()
