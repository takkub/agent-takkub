"""Gemini/agy ingest adapter — WRAPS `gemini_helper.py`'s transcript
resolvers (`resolve_antigravity_transcript` for the 2026-08 agy store,
`resolve_gemini_jsonl_for_cwd` for the legacy `~/.gemini/tmp/.../chats/`
one) AND its record parsers (`parse_antigravity_record_message`,
`gemini_record_messages`, `gemini_message_text`) — the same functions the
Remote mirror (`remote/notify.py`) uses, so a schema change is fixed in one
place (2026-10-24 review D removed the old duplicate here). New-store is
tried first, same precedence `remote/notify.py::_resolve_gemini_jsonl_path`
uses (a machine that has used both keeps the old, frozen files around).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub import gemini_helper
from agent_takkub.core.models.conversation import MessageRole

from ._jsonl_tail import read_complete_lines
from .base import IngestBatch, IngestedMessage

provider_id = "gemini"

_KIND_ROLE = {"me": MessageRole.USER, "lead": MessageRole.ASSISTANT}


def _antigravity_message(rec: dict) -> IngestedMessage | None:
    parsed = gemini_helper.parse_antigravity_record_message(rec)
    if parsed is None:
        return None
    return IngestedMessage(role=_KIND_ROLE[parsed[0]], text=parsed[1])


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    path = gemini_helper.resolve_antigravity_transcript(cwd, session_id)
    if path is None:
        path = gemini_helper.resolve_gemini_jsonl_for_cwd(cwd, session_id)
    return str(path) if path is not None else None


def read_new(source_id: str, cursor: str | None) -> IngestBatch:
    chunk = read_complete_lines(Path(source_id), cursor)
    if chunk is None:
        return IngestBatch(source_id, [], cursor or "0")
    lines, next_cursor = chunk
    messages: list[IngestedMessage] = []
    seen_legacy_ids: set[str] = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        antigravity = _antigravity_message(rec)
        if antigravity is not None:
            messages.append(antigravity)
            continue
        for m in gemini_helper.gemini_record_messages(rec):
            mid = str(m.get("id", "")).strip()
            if mid:
                if mid in seen_legacy_ids:
                    continue
                seen_legacy_ids.add(mid)
            mtype = m.get("type")
            text = gemini_helper.gemini_message_text(m)
            if not text:
                continue
            if mtype == "user" and not text.startswith("<session_context>"):
                messages.append(IngestedMessage(role=MessageRole.USER, text=text))
            elif mtype == "gemini":
                messages.append(IngestedMessage(role=MessageRole.ASSISTANT, text=text))
    return IngestBatch(source_id, messages, next_cursor)
