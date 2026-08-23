"""Append-only migration journal — one line per step-outcome, replayed in
reverse by `MigrationEngine.rollback()`. Built on `core.storage.jsonl_store`
(same atomic-append / corruption-tolerant-read guarantees), stored at
`core_store_path("migration_journal")` — no new home declared.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..storage.jsonl_store import JsonlStore
from ..storage.paths import core_store_path

_STORE_NAME = "migration_journal"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    step_id: str
    action: str  # "apply" | "rollback"
    ok: bool
    detail: str
    timestamp: float


class MigrationJournal:
    def __init__(self, store: JsonlStore | None = None) -> None:
        self._store = store or JsonlStore(core_store_path(_STORE_NAME))

    @property
    def store_path(self) -> Path:
        """Where this journal's own file lives — `CoreInternalStoreStep`
        (#360) needs this to exclude the journal it is writing to right now
        from the tree it copies into `v2/system/`."""
        return self._store.path

    def record(self, step_id: str, action: str, ok: bool, detail: str = "") -> JournalEntry:
        entry = JournalEntry(step_id, action, ok, detail, time.time())
        self._store.append(
            {
                "step_id": entry.step_id,
                "action": entry.action,
                "ok": entry.ok,
                "detail": entry.detail,
                "timestamp": entry.timestamp,
            }
        )
        return entry

    def all_entries(self) -> list[JournalEntry]:
        result = self._store.read_all()
        return [
            JournalEntry(
                str(r.get("step_id", "")),
                str(r.get("action", "")),
                bool(r.get("ok", False)),
                str(r.get("detail", "")),
                float(r.get("timestamp", 0.0)),
            )
            for r in result.records
        ]

    def applied_step_ids(self) -> list[str]:
        """Step ids with a successful, not-yet-rolled-back apply, in apply
        order — what `MigrationEngine.rollback()` would need to undo."""
        applied: list[str] = []
        for e in self.all_entries():
            if e.action == "apply" and e.ok:
                if e.step_id not in applied:
                    applied.append(e.step_id)
            elif e.action == "rollback" and e.ok and e.step_id in applied:
                applied.remove(e.step_id)
        return applied
