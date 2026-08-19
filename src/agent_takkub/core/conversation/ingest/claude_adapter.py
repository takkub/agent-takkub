"""Claude ingest adapter — WRAPS `chatlog_scanner.py`'s record parsing
(`is_conversation_record`/`role_of`/`extract_text`) and `token_meter.py`'s
uuid-exact / newest-mtime session resolvers. Neither module is modified;
this file only adds the byte-offset incremental walk neither one needs for
its existing (live-tail / one-shot-scan) callers.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub import chatlog_scanner
from agent_takkub.core.models.conversation import MessageRole
from agent_takkub.token_meter import find_latest_session, find_session_by_uuid

from .base import IngestBatch, IngestedMessage

provider_id = "claude"


def resolve_source(
    cwd: str, session_id: str | None, *, config_dir: str | None = None
) -> str | None:
    path = find_session_by_uuid(cwd, session_id, config_dir) if session_id else None
    if path is None:
        path = find_latest_session(cwd, config_dir=config_dir)
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
        if not isinstance(rec, dict) or not chatlog_scanner.is_conversation_record(rec):
            continue
        role_raw = chatlog_scanner.role_of(rec)
        if role_raw not in ("user", "assistant"):
            continue
        text = chatlog_scanner.extract_text(rec)
        if not text:
            continue
        dt = chatlog_scanner.record_timestamp(rec)
        messages.append(
            IngestedMessage(
                role=MessageRole.USER if role_raw == "user" else MessageRole.ASSISTANT,
                text=text,
                created_at=dt.timestamp() if dt is not None else None,
            )
        )
    return IngestBatch(source_id, messages, str(size))
