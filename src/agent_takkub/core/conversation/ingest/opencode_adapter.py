"""OpenCode ingest adapter — WRAPS `opencode_helper.py`'s
`resolve_opencode_session`/`read_opencode_session_messages` (already
normalized to `{"text": str, "kind": "me"|"lead"}` — no schema-drift risk to
duplicate here, unlike codex/gemini). Not modified.

OpenCode keeps every project's sessions in ONE shared sqlite db with no byte
offset to seek by, so the cursor here is a MESSAGE COUNT, not a byte offset
(documented ceiling, `IngestBatch.next_cursor`): `read_opencode_session_messages`
is called for the full session each time and the result sliced past the
last-seen count. Fine for a session's normal size; a session that grows past
`_MAX_MESSAGES` between two ingests could re-read but never double-count
(the slice is still exact) — it would only start silently dropping the
oldest tail, which `opencode_helper`'s own `limit` already does today.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub.core.models.conversation import MessageRole
from agent_takkub.opencode_helper import read_opencode_session_messages, resolve_opencode_session

from .base import IngestBatch, IngestedMessage

provider_id = "opencode"

_SEP = "\x1f"
_MAX_MESSAGES = 100_000

_KIND_TO_ROLE = {"me": MessageRole.USER, "lead": MessageRole.ASSISTANT}


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    resolved = resolve_opencode_session(cwd, session_id)
    if resolved is None:
        return None
    db_path, sid = resolved
    return f"{db_path}{_SEP}{sid}"


def read_new(source_id: str, cursor: str | None) -> IngestBatch:
    db_path, _, sid = source_id.partition(_SEP)
    seen = int(cursor) if cursor else 0
    rows = read_opencode_session_messages(Path(db_path), sid, _MAX_MESSAGES)
    new_rows = rows[seen:]
    messages = [
        IngestedMessage(role=_KIND_TO_ROLE[row["kind"]], text=row["text"])
        for row in new_rows
        if row.get("kind") in _KIND_TO_ROLE and row.get("text")
    ]
    return IngestBatch(source_id, messages, str(len(rows)))
