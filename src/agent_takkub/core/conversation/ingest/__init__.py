"""Provider-transcript ingest registry (plan §5.3, epic #309 Phase 6).

Registered: claude / codex / gemini / opencode / cursor / kimi — all 6
providers, epic #309's last `core.conversation.ingest` gap now closed
(`docs/v2/2.0.0-migration-plan.md` §1.1, investigated + built 2026-08-22).

cursor DOES have a `remote/notify.py::_HISTORY_SCANNERS` entry (that
registry covers claude/codex/cursor/gemini/opencode), so the remote/mobile
history path already worked for it — `cursor_adapter.py` closed the
remaining gap (the V2 `core.conversation.ingest` adapter was missing).

kimi has NEITHER a `_HISTORY_SCANNERS` entry nor `ProviderSpec.
supports_remote_history` — that (#103) is a DIFFERENT, still-open gap in a
different consumer (the mobile remote-mirror system) and is intentionally
untouched here. What investigation found is that kimi CLI 1.49.0 DOES
persist a real, locally-readable, per-work-dir session store (`~/.kimi/
kimi.json` + `~/.kimi/sessions/<hash>/<id>/wire.jsonl`) — see
`kimi_helper.py`'s module docstring for the full trail (verified against a
real `~/.kimi` on this machine and against kimi-cli's own installed typed
source, not a fixture) — so `kimi_adapter.py` is a real adapter, built the
same WRAP shape as `cursor_adapter.py`, not a "stays unsupported" closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from agent_takkub.core.conversation.store import ConversationStore
from agent_takkub.core.models.conversation import Message

from . import (
    claude_adapter,
    codex_adapter,
    cursor_adapter,
    gemini_adapter,
    kimi_adapter,
    opencode_adapter,
)
from .cursor_store import get_cursor, set_cursor

_ADAPTERS: dict[str, ModuleType] = {
    "claude": claude_adapter,
    "codex": codex_adapter,
    "gemini": gemini_adapter,
    "opencode": opencode_adapter,
    "cursor": cursor_adapter,
    "kimi": kimi_adapter,
}


def adapter_for(provider_id: str) -> ModuleType | None:
    return _ADAPTERS.get(provider_id)


def supported_providers() -> tuple[str, ...]:
    return tuple(_ADAPTERS)


@dataclass(frozen=True, slots=True)
class IngestResult:
    ingested: int
    source_id: str | None
    skipped_no_source: bool = False


def ingest_provider_transcript(
    provider_id: str,
    project_id: str | None,
    conversation_id: str,
    *,
    cwd: str,
    session_id: str | None = None,
    store: ConversationStore | None = None,
    cursor_path: Path | None = None,
) -> IngestResult:
    """Read whatever's new in *provider_id*'s transcript for (cwd, session_id)
    and append it to the conversation. Idempotent — safe to call repeatedly
    (e.g. from a checkpoint hook): the persisted cursor guarantees a line
    already ingested is never appended twice.

    `cursor_path` overrides where the cursor is persisted — dependency
    injection for test isolation, same pattern as
    `core.accounts.switch_log.log_switch(..., store=...)`; `None` uses the
    real default (`core.storage.paths.core_home()`).
    """
    adapter = adapter_for(provider_id)
    if adapter is None:
        return IngestResult(0, None, skipped_no_source=True)
    source_id = adapter.resolve_source(cwd, session_id)
    if source_id is None:
        return IngestResult(0, None, skipped_no_source=True)
    cs = store or ConversationStore()
    cursor = get_cursor(provider_id, source_id, path=cursor_path)
    batch = adapter.read_new(source_id, cursor)
    for msg in batch.messages:
        cs.append_message(
            project_id,
            conversation_id,
            role=msg.role,
            text=msg.text,
            provider_id=provider_id,
            source="live_pty",
            created_at=msg.created_at,
        )
    if batch.next_cursor != cursor:
        set_cursor(provider_id, source_id, batch.next_cursor, path=cursor_path)
    return IngestResult(len(batch.messages), source_id)


__all__ = [
    "IngestResult",
    "Message",
    "adapter_for",
    "ingest_provider_transcript",
    "supported_providers",
]
