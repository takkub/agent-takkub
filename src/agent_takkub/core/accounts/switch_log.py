"""Account-switch event log — plan scope for Phase 3: "account switch =
log event ไว้ก่อน (checkpoint มาขั้น 6)". Records WHICH account an
`AccountSelector` moved to and why, without yet wiring a real spawn-time
credential swap or a Conversation-V2 checkpoint (that's Phase 6). Append-only
JsonlStore, same pattern as every other `core.storage` writer.
"""

from __future__ import annotations

import time
from typing import Any

from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.paths import core_store_path


def log_switch(
    pool_id: str,
    provider_id: str,
    from_account_id: str | None,
    to_account_id: str | None,
    *,
    reason: str,
    store: JsonlStore | None = None,
) -> None:
    record: dict[str, Any] = {
        "ts": time.time(),
        "pool_id": pool_id,
        "provider_id": provider_id,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "reason": reason,
    }
    (store or JsonlStore(core_store_path("account_switches"))).append(record)


def read_switches(store: JsonlStore | None = None) -> list[dict[str, Any]]:
    records, _ = (store or JsonlStore(core_store_path("account_switches"))).read_all()
    return records
