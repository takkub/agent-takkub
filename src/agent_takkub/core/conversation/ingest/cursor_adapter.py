"""Cursor ingest adapter — WRAPS `cursor_helper.py`'s transcript resolver
and record parser (`resolve_cursor_jsonl_for_cwd`, `parse_cursor_record_message`)
directly; neither is modified or duplicated.

Unlike `codex_adapter.py`, this is a FULL wrap, not a wrap-plus-duplicate:
`cursor_helper.py` (like `codex_helper.py`) has no PyQt6 import, so `core.*`
can import it under the `core-is-bottom-layer` contract — and unlike codex,
cursor's record-parsing logic already lives entirely inside that Qt-free
helper (`parse_cursor_record_message`, also used by `remote/notify.py`'s
`_read_recent_cursor_messages`/`_cursor_live_text_blocks`/`_cursor_live_users`),
so there is nothing to duplicate. A future Cursor CLI schema change only
needs a fix in one place, and `tests/test_core_conversation_ingest.py`'s
real-store smoke test exercises that same shared function.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.core.models.conversation import MessageRole
from agent_takkub.cursor_helper import (
    parse_cursor_record_message,
    resolve_cursor_jsonl_for_cwd,
)

from .base import IngestBatch, IngestedMessage

provider_id = "cursor"

_ROLE_MAP = {"me": MessageRole.USER, "lead": MessageRole.ASSISTANT}


def _parse_record(rec: dict) -> IngestedMessage | None:
    parsed = parse_cursor_record_message(rec)
    if parsed is None:
        return None
    kind, text = parsed
    role = _ROLE_MAP.get(kind)
    if role is None or not text:
        return None
    return IngestedMessage(role=role, text=text)


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    path = resolve_cursor_jsonl_for_cwd(cwd, session_id)
    return str(path) if path is not None else None


def read_new(source_id: str, cursor: str | None) -> IngestBatch:
    path = Path(source_id)
    offset = int(cursor) if cursor else 0
    try:
        size = path.stat().st_size
    except OSError:
        return IngestBatch(source_id, [], cursor or "0")
    if offset >= size:
        return IngestBatch(source_id, [], str(size))
    with open(path, "rb") as f:
        f.seek(max(0, offset))
        raw = f.read()
    messages: list[IngestedMessage] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        parsed = _parse_record(rec)
        if parsed is not None:
            messages.append(parsed)
    return IngestBatch(source_id, messages, str(size))
