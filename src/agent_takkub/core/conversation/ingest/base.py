"""`IngestAdapter` Protocol — the seam every provider-specific ingest module
(`claude_adapter.py`, `codex_adapter.py`, …) satisfies (plan §5.3, "WRAP the
existing reader, don't modify it").

`IngestedMessage` is intentionally NOT `core.models.conversation.Message` —
an adapter doesn't know the conversation id or the store-assigned message id
yet, only role/text/provider/timestamp. `ConversationStore.append_message`
is what turns one of these into a real `Message`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_takkub.core.models.conversation import MessageRole


@dataclass(frozen=True, slots=True)
class IngestedMessage:
    role: MessageRole
    text: str
    created_at: float | None = None


@dataclass(frozen=True, slots=True)
class IngestBatch:
    source_id: str
    messages: list[IngestedMessage]
    next_cursor: str


class IngestAdapter(Protocol):
    provider_id: str

    def resolve_source(self, cwd: str, session_id: str | None) -> str | None:
        """A stable identity for the transcript this (cwd, session) maps
        to — a file path for a JSONL-backed provider, `"<db>::<session>"`
        for a shared-database one. `None` when nothing matches yet (fresh
        pane, no transcript written)."""
        ...

    def read_new(self, source_id: str, cursor: str | None) -> IngestBatch:
        """Messages appended since `cursor` (that adapter's own cursor
        format — a byte offset for JSONL, a message count for a shared DB).
        `cursor=None` means "never ingested before" and must read from the
        start, never skip straight to "recent only" the way the live-mirror
        readers in `remote/notify.py` do — a checkpoint's first ingest needs
        the full history, not just a tail."""
        ...
