"""Gemini/agy ingest adapter — WRAPS `gemini_helper.py`'s transcript
resolvers (`resolve_antigravity_transcript` for the 2026-08 agy store,
`resolve_gemini_jsonl_for_cwd` for the legacy `~/.gemini/tmp/.../chats/`
one); neither is modified. New-store is tried first, same precedence
`remote/notify.py::_resolve_gemini_jsonl_path` uses and documents (a machine
that has used both keeps the old, frozen files around).

`_parse_record`'s two record shapes (agy `USER_INPUT`/`PLANNER_RESPONSE`,
and legacy `$set.messages` snapshot / incremental `{"type": "user"|"gemini"}`)
are a DELIBERATE, DOCUMENTED duplicate of `remote/notify.py`'s
`_antigravity_record_message`/`_gemini_record_messages`/`_gemini_message_text`
— same `core-is-bottom-layer` boundary reason as `codex_adapter.py`'s
docstring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_takkub import gemini_helper
from agent_takkub.core.models.conversation import MessageRole

from .base import IngestBatch, IngestedMessage

provider_id = "gemini"

_ANTIGRAVITY_KINDS = {"USER_INPUT": MessageRole.USER, "PLANNER_RESPONSE": MessageRole.ASSISTANT}
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


def _antigravity_message(rec: dict) -> IngestedMessage | None:
    role = _ANTIGRAVITY_KINDS.get(str(rec.get("type") or "").strip())
    if role is None:
        return None
    content = rec.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if role is MessageRole.USER:
        match = _USER_REQUEST_RE.search(content)
        if match is None:
            return None
        content = match.group(1)
    text = content.strip()
    return IngestedMessage(role=role, text=text) if text else None


def _legacy_message_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = [
        item if isinstance(item, str) else item.get("text", "")
        for item in content
        if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("text"), str))
    ]
    return "\n".join(p.strip() for p in parts if p.strip()).strip()


def _legacy_messages(rec: dict) -> list[dict]:
    patch = rec.get("$set")
    if isinstance(patch, dict) and isinstance(patch.get("messages"), list):
        return [m for m in patch["messages"] if isinstance(m, dict)]
    if rec.get("id") and rec.get("type") in ("user", "gemini"):
        return [rec]
    return []


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    path = gemini_helper.resolve_antigravity_transcript(cwd, session_id)
    if path is None:
        path = gemini_helper.resolve_gemini_jsonl_for_cwd(cwd, session_id)
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
    seen_legacy_ids: set[str] = set()
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
        antigravity = _antigravity_message(rec)
        if antigravity is not None:
            messages.append(antigravity)
            continue
        for m in _legacy_messages(rec):
            mid = str(m.get("id", "")).strip()
            if mid:
                if mid in seen_legacy_ids:
                    continue
                seen_legacy_ids.add(mid)
            mtype = m.get("type")
            text = _legacy_message_text(m)
            if not text:
                continue
            if mtype == "user" and not text.startswith("<session_context>"):
                messages.append(IngestedMessage(role=MessageRole.USER, text=text))
            elif mtype == "gemini":
                messages.append(IngestedMessage(role=MessageRole.ASSISTANT, text=text))
    return IngestBatch(source_id, messages, str(size))
