"""`ConversationStore` — Takkub-owned message store (plan §5.2, epic #309
Phase 6). One directory per conversation under
`core.storage.paths.conversation_dir()`:

    conversations/<project_id>/<conversation_id>/
        conversation.json   # Conversation metadata (id, task_id, …)
        messages.jsonl       # true-append Message stream (+ rotated parts)
        messages.1.jsonl      # oldest rotated part, ascending index
        bindings.jsonl        # ProviderSessionBinding stream (pointers only)

`ProviderSessionBinding` stays a *pointer* (module docstring on
`core.models.conversation`) — the provider's own store is still the source
of truth for anything this layer hasn't ingested yet; `messages.jsonl` only
holds what an ingest adapter or `facade.on_pane_done` actually wrote here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from pathlib import Path

from agent_takkub.core.models.conversation import (
    Conversation,
    Message,
    MessageRole,
    ProviderSessionBinding,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.legacy_reader import read_json
from agent_takkub.core.storage.paths import conversations_root

from ._json_io import write_json_atomic

# ponytail: rotate at 5 MB — comfortably above a normal task's transcript,
# small enough that a read_messages() call never has to load an unbounded
# file. No compaction/merge of rotated parts yet (gap — see phase6-report).
_ROTATE_AT_BYTES = 5 * 1024 * 1024


def conversation_id_for(project_id: str | None, role: str) -> str:
    """Deterministic id for the ONE conversation this phase tracks per
    (project, role) — e.g. a project's Lead pane, or one teammate role.
    Not a general multi-conversation-per-project scheme yet (gap — see
    phase6-report): good enough for the `done()`/remote-mirror hooks, which
    both need to agree on the same id without a separate lookup table.
    """
    return f"conv-{project_id or '_no_project'}-{role}"


class ConversationStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or conversations_root()

    def conversation_dir(self, project_id: str | None, conversation_id: str) -> Path:
        """Public so `checkpoint.py`/`facade.py` derive paths from THIS
        store's root instead of re-deriving from `core.storage.paths`
        directly — a store constructed with a custom `root` (tests, a
        future per-user root) must stay the single source of truth for
        where a conversation's files live."""
        return self._root / (project_id or "_no_project") / conversation_id

    def _dir(self, project_id: str | None, conversation_id: str) -> Path:
        return self.conversation_dir(project_id, conversation_id)

    def _meta_path(self, project_id: str | None, conversation_id: str) -> Path:
        return self._dir(project_id, conversation_id) / "conversation.json"

    def _messages_path(self, project_id: str | None, conversation_id: str) -> Path:
        return self._dir(project_id, conversation_id) / "messages.jsonl"

    def _bindings_path(self, project_id: str | None, conversation_id: str) -> Path:
        return self._dir(project_id, conversation_id) / "bindings.jsonl"

    # ── conversation lifecycle ───────────────────────────────────────────

    def get_conversation(self, project_id: str | None, conversation_id: str) -> Conversation | None:
        raw = read_json(self._meta_path(project_id, conversation_id))
        if not raw:
            return None
        return Conversation(**raw)

    def get_or_create(
        self,
        conversation_id: str,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_instance_id: str | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Conversation:
        existing = self.get_conversation(project_id, conversation_id)
        if existing is not None:
            return existing
        conv = Conversation(
            id=conversation_id,
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            created_at=time.time(),
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        write_json_atomic(self._meta_path(project_id, conversation_id), asdict(conv))
        return conv

    # ── messages ──────────────────────────────────────────────────────────

    def _rotate_if_needed(self, project_id: str | None, conversation_id: str) -> None:
        path = self._messages_path(project_id, conversation_id)
        try:
            if not path.exists() or path.stat().st_size < _ROTATE_AT_BYTES:
                return
        except OSError:
            return
        directory = self._dir(project_id, conversation_id)
        n = 1
        while (directory / f"messages.{n}.jsonl").exists():
            n += 1
        path.rename(directory / f"messages.{n}.jsonl")

    def append_message(
        self,
        project_id: str | None,
        conversation_id: str,
        *,
        role,
        text: str,
        provider_id: str | None = None,
        source: str = "live_pty",
        created_at: float | None = None,
        message_id: str | None = None,
    ) -> Message:
        self._rotate_if_needed(project_id, conversation_id)
        message = Message(
            id=message_id or uuid.uuid4().hex,
            conversation_id=conversation_id,
            role=role,
            text=text,
            created_at=created_at if created_at is not None else time.time(),
            provider_id=provider_id,
            source=source,
            project_id=project_id,
        )
        store = JsonlStore(self._messages_path(project_id, conversation_id), true_append=True)
        record = asdict(message)
        record["role"] = str(message.role)
        store.append(record)
        return message

    def read_messages(self, project_id: str | None, conversation_id: str) -> list[Message]:
        directory = self._dir(project_id, conversation_id)
        parts: list[Path] = []
        n = 1
        while (directory / f"messages.{n}.jsonl").exists():
            parts.append(directory / f"messages.{n}.jsonl")
            n += 1
        parts.append(self._messages_path(project_id, conversation_id))
        out: list[Message] = []
        for part in parts:
            records, _ = JsonlStore(part).read_all()
            for rec in records:
                rec = dict(rec)
                rec["role"] = MessageRole(rec["role"])
                out.append(Message(**rec))
        return out

    # ── provider session bindings (pointers, never source of truth) ────────

    def bind_provider_session(
        self,
        project_id: str | None,
        conversation_id: str,
        *,
        provider_id: str,
        session_id: str,
        cwd: str | None = None,
        account_id: str | None = None,
    ) -> ProviderSessionBinding:
        """Upsert: a repeat call with the same (provider_id, session_id, cwd,
        account_id) as the most recent binding is a no-op — a long-lived
        conversation calls this on every `done()`, and `bindings.jsonl` is a
        true-append stream, so without this check it would grow one
        identical row per done() instead of only recording real changes."""
        existing = self.provider_bindings(project_id, conversation_id)
        if existing:
            last = existing[-1]
            if (
                last.provider_id == provider_id
                and last.session_id == session_id
                and last.cwd == cwd
                and last.account_id == account_id
            ):
                return last
        binding = ProviderSessionBinding(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            provider_id=provider_id,
            session_id=session_id,
            cwd=cwd,
            account_id=account_id,
            bound_at=time.time(),
            project_id=project_id,
        )
        store = JsonlStore(self._bindings_path(project_id, conversation_id), true_append=True)
        store.append(asdict(binding))
        return binding

    def provider_bindings(
        self, project_id: str | None, conversation_id: str
    ) -> list[ProviderSessionBinding]:
        records, _ = JsonlStore(self._bindings_path(project_id, conversation_id)).read_all()
        return [ProviderSessionBinding(**rec) for rec in records]
