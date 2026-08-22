"""Kimi ingest adapter — WRAPS `kimi_helper.py`'s session resolver and wire
record parser (`resolve_kimi_session_dir`, `parse_kimi_wire_record`)
directly; neither is modified or duplicated. See `kimi_helper.py`'s module
docstring for the investigation (`docs/v2/2.0.0-migration-plan.md` §1.1,
epic #309) that grounds this in kimi-cli's own installed source rather than
a guess, and the one open gap (no live-model example captured on this
machine to verify the assistant-`TextPart` branch against).

Same full-wrap shape as `cursor_adapter.py`: `kimi_helper.py` has no PyQt6
import, so `core.*` may import it under the `core-is-bottom-layer` contract,
and the record parser already lives in that one Qt-free module.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.core.models.conversation import MessageRole
from agent_takkub.kimi_helper import parse_kimi_wire_record, resolve_kimi_session_dir

from .base import IngestBatch, IngestedMessage

provider_id = "kimi"

_ROLE_MAP = {"me": MessageRole.USER, "lead": MessageRole.ASSISTANT}


def _parse_record(rec: dict) -> IngestedMessage | None:
    parsed = parse_kimi_wire_record(rec)
    if parsed is None:
        return None
    kind, text = parsed
    role = _ROLE_MAP.get(kind)
    if role is None or not text:
        return None
    return IngestedMessage(role=role, text=text)


def resolve_source(cwd: str, session_id: str | None) -> str | None:
    session_dir = resolve_kimi_session_dir(cwd, session_id)
    if session_dir is None:
        return None
    return str(session_dir / "wire.jsonl")


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
