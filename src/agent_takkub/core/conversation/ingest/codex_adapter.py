"""Codex ingest adapter — WRAPS `codex_helper.py`'s session-root/cwd-match
helpers (`codex_sessions_root`, `normalize_codex_cwd`, `read_codex_session_meta`)
AND its record parser (`parse_codex_record_message`, both the <= 0.146 and the
0.147 `item_completed` schema) — the same function the Remote mirror
(`remote/notify.py`) uses. It used to be a documented duplicate here and the
two drifted (this copy dropped `local_image` blocks); 2026-10-24 review D.
Both schema branches are exercised by `tests/test_core_conversation_ingest.py`
so a future schema flip fails a **core** test, not just the remote-mirror one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_takkub.codex_helper import (
    codex_archived_sessions_root,
    codex_sessions_root,
    normalize_codex_cwd,
    parse_codex_record_message,
    read_codex_session_meta,
)
from agent_takkub.core.models.conversation import MessageRole

from ._jsonl_tail import read_complete_lines
from .base import IngestBatch, IngestedMessage

provider_id = "codex"

_KIND_ROLE = {"me": MessageRole.USER, "lead": MessageRole.ASSISTANT}


def _record_epoch(rec: dict) -> float | None:
    """Codex rollout records carry a top-level ISO8601 `timestamp` (same
    field/format claude's does — confirmed live, `usage_ledger.py`'s codex
    importer reads the identical field). None when missing/unparseable,
    never fabricated (#517). A parsed value with no offset (B-L7,
    2026-09-07) is pinned to UTC before `.timestamp()` — otherwise Python
    treats a naive datetime as local time and the epoch drifts with the
    host machine's timezone."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _parse_record(rec: dict) -> IngestedMessage | None:
    parsed = parse_codex_record_message(rec)
    if parsed is None:
        return None
    return IngestedMessage(
        role=_KIND_ROLE[parsed[0]], text=parsed[1], created_at=_record_epoch(rec)
    )


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
    chunk = read_complete_lines(Path(source_id), cursor)
    if chunk is None:
        return IngestBatch(source_id, [], cursor or "0")
    lines, next_cursor = chunk
    messages: list[IngestedMessage] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        parsed = _parse_record(rec)
        if parsed is not None:
            messages.append(parsed)
    return IngestBatch(source_id, messages, next_cursor)
