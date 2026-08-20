"""Codex ingest adapter — WRAPS `codex_helper.py`'s session-root/cwd-match
helpers (`codex_sessions_root`, `normalize_codex_cwd`, `read_codex_session_meta`,
`resolve_codex_jsonl_for_cwd`); none of them are modified.

`_parse_record` below is a DELIBERATE, DOCUMENTED duplicate of
`remote/notify.py::_codex_record_message`'s two-schema parsing (codex 0.147
replaced `event_msg.agent_message`/`.user_message` with
`event_msg.item_completed` — the schema-drift lesson from #206/D3, see
`docs/v2/CURRENT_ARCHITECTURE_AUDIT.md` §2.9). It cannot import
`remote/notify.py` directly — that module pulls in `PyQt6.QtCore`, and
`core.*` must stay importable standalone (`core-is-bottom-layer` contract).
Both schema branches are exercised by `tests/test_core_conversation_ingest.py`
so a future schema flip fails a **core** test, not just the remote-mirror one.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.codex_helper import (
    codex_archived_sessions_root,
    codex_sessions_root,
    normalize_codex_cwd,
    read_codex_session_meta,
)
from agent_takkub.core.models.conversation import MessageRole

from .base import IngestBatch, IngestedMessage

provider_id = "codex"

_ITEM_KINDS = {"AgentMessage": MessageRole.ASSISTANT, "UserMessage": MessageRole.USER}


def _item_text(item: dict) -> str:
    blocks = item.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").strip().lower() != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _parse_record(rec: dict) -> IngestedMessage | None:
    if rec.get("type") != "event_msg":
        return None
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    if ptype == "item_completed":  # codex >= 0.147 (TUI)
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        role = _ITEM_KINDS.get(str(item.get("type") or "").strip())
        if role is None:
            return None
        text = _item_text(item)
        return IngestedMessage(role=role, text=text) if text else None
    if ptype == "agent_message":  # codex <= 0.146, and `codex exec` on 0.147
        role = MessageRole.ASSISTANT
    elif ptype == "user_message":
        role = MessageRole.USER
    else:
        return None
    text = payload.get("message")
    if not isinstance(text, str) or not text.strip():
        return None
    return IngestedMessage(role=role, text=text.strip())


def _scan_root_for_cwd(root: Path, wanted_cwd: str, wanted_id: str) -> str | None:
    if not root.is_dir():
        return None
    try:
        candidates = sorted(
            root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    for path in candidates:
        meta = read_codex_session_meta(path)
        if normalize_codex_cwd(meta.get("cwd")) != wanted_cwd:
            continue
        if wanted_id:
            meta_id = str(meta.get("id") or meta.get("session_id") or "").strip()
            if meta_id != wanted_id:
                continue
        return str(path)
    return None


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    wanted_cwd = normalize_codex_cwd(cwd)
    if not wanted_cwd:
        return None
    wanted_id = str(session_id or "").strip()
    found = _scan_root_for_cwd(codex_sessions_root(), wanted_cwd, wanted_id)
    if found is not None:
        return found
    if wanted_id:
        # `codex archive` (0.148+) moves the rollout file out of `sessions/`
        # into a flat `archived_sessions/` dir — only worth checking for an
        # exact id match; a fresh/live session (no id yet) can't already be
        # archived, so the no-id "newest for cwd" path never needs this.
        found = _scan_root_for_cwd(codex_archived_sessions_root(), wanted_cwd, wanted_id)
    return found


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
