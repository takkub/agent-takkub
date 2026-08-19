"""Conversation vocabulary (Phase 5 target — Takkub-owned message store).

`ProviderSessionBinding` is a **pointer**, not the source of truth: the real
transcript still lives in the provider's own store (`.claude/…`, codex's
session dir, …) until Phase 5 ingests it. Named to avoid the existing
`_task_handoff_pointer` / `HandoffRecord` vocabulary collision flagged in
the plan (§3.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Message:
    """One normalized turn, provider-agnostic (plan §5.1 ingest contract).

    `source` names where the record came from — `"live_pty"` (transcript
    ingest from a provider's own store) vs `"done_note"` (the summary an
    agent wrote via `takkub done`) — so a reader can tell an ingested
    transcript line apart from cockpit-authored summary text without
    guessing from content shape.
    """

    id: str
    conversation_id: str
    role: MessageRole
    text: str
    created_at: float | None = None
    provider_id: str | None = None
    source: str = "live_pty"
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    task_id: str | None = None
    agent_instance_id: str | None = None
    created_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSessionBinding:
    """Pointer from a `Conversation` to the provider's own session id —
    never the source of truth for message content."""

    id: str
    conversation_id: str
    provider_id: str
    session_id: str
    bound_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    id: str
    conversation_id: str
    summary: str = ""
    created_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
