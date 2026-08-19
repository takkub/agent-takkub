"""Persistence for the Second Brain: one append-only `JsonlStore` per
project, plus a `_global` bucket for `Scope.GLOBAL`/`Scope.USER` records
that outlive any single project — all living under
`core_home()/brain/` (`core.storage.paths`, plan §3.4).

The log is event-sourced, not row-updated: `JsonlStore` only appends
(`core/storage/jsonl_store.py`'s docstring — no in-place update primitive
exists), so "updating" a record (e.g. flipping it to SUPERSEDED) means
appending a new line with the same `id`. `load_latest` folds the log by
`id`, keeping the LAST line for each — since `append` always adds at the
end, iteration order already gives "last write wins" with no version
comparison needed.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from agent_takkub.core.models.memory import (
    Confidence,
    MemoryKind,
    MemoryRecord,
    RecordStatus,
    Scope,
    Trust,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.paths import core_home

_GLOBAL_BUCKET = "_global"


def _safe_project(name: str | None) -> str:
    """Sanitize a project name into one safe path segment (mirrors
    `role_memory._safe`'s traversal-proofing without depending on that
    module's private helper)."""
    if not name:
        return _GLOBAL_BUCKET
    s = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    while ".." in s:
        s = s.replace("..", "_")
    s = s.strip(".")
    return s or _GLOBAL_BUCKET


def brain_store_path(project: str | None) -> Path:
    return core_home() / "brain" / f"{_safe_project(project)}.jsonl"


def _record_to_dict(record: MemoryRecord) -> dict:
    d = dataclasses.asdict(record)
    d["kind"] = record.kind.value
    d["scope"] = record.scope.value
    d["trust"] = record.trust.value
    d["confidence"] = record.confidence.value
    d["status"] = record.status.value
    return d


def _record_from_dict(d: dict) -> MemoryRecord:
    kwargs = dict(d)
    kwargs["kind"] = MemoryKind(d["kind"])
    kwargs["scope"] = Scope(d["scope"])
    kwargs["trust"] = Trust(d["trust"])
    kwargs["confidence"] = Confidence(d["confidence"])
    kwargs["status"] = RecordStatus(d.get("status", RecordStatus.ACTIVE.value))
    return MemoryRecord(**kwargs)


class BrainStore:
    """Event-log store for one project's (or the global) memory records."""

    def __init__(self, project: str | None = None) -> None:
        self.project = project
        self._jsonl = JsonlStore(brain_store_path(project))

    def append_event(self, record: MemoryRecord) -> None:
        self._jsonl.append(_record_to_dict(record))

    def load_latest(self) -> list[MemoryRecord]:
        """Every record id's most-recently-appended state (see module
        docstring) — includes SUPERSEDED records, callers filter for
        `status == ACTIVE` when they only want current facts."""
        result = self._jsonl.read_all()
        by_id: dict[str, MemoryRecord] = {}
        for row in result.records:
            try:
                rec = _record_from_dict(row)
            except (KeyError, ValueError):
                continue  # corrupt/foreign row — skip, never raise
            by_id[rec.id] = rec
        return list(by_id.values())

    def load_active(self) -> list[MemoryRecord]:
        return [r for r in self.load_latest() if r.status == RecordStatus.ACTIVE]


__all__ = ["BrainStore", "brain_store_path"]
