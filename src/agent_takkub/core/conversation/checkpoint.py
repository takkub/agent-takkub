"""`CheckpointManager` — snapshot a conversation before a provider switch,
account switch, provider/app update, major task boundary, or graceful
shutdown (plan §7). Layout matches the task scope exactly:

    checkpoint-NNNNN/
        summary.json           # RollingSummary snapshot at checkpoint time
        working-context.json   # last N messages, rendered
        tasks.json             # caller-supplied (Task Ledger rows, etc.)
        git-state.json         # caller-supplied (digest_facts-shaped)
        provider-binding.json  # ProviderSessionBinding rows for this conversation
        runtime-state.json     # caller-supplied free-form dict

`tasks`/`git_state`/`runtime_state` are the caller's job to gather (Task
Ledger / `digest_facts` / pane state) — this module never reaches into the
engine to collect them itself, keeping `core.*` importable standalone.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_takkub.core.models.conversation import Checkpoint
from agent_takkub.core.storage.legacy_reader import read_json

from ._json_io import write_json_atomic
from .store import ConversationStore
from .summary import load_summary

_WORKING_CONTEXT_TAIL = 20
_FILES = (
    "summary",
    "working-context",
    "tasks",
    "git-state",
    "provider-binding",
    "runtime-state",
)


class CheckpointManager:
    def __init__(self, store: ConversationStore | None = None) -> None:
        self._store = store or ConversationStore()

    def _conv_dir(self, project_id: str | None, conversation_id: str) -> Path:
        return self._store.conversation_dir(project_id, conversation_id)

    def _checkpoints_dir(self, project_id: str | None, conversation_id: str) -> Path:
        return self._conv_dir(project_id, conversation_id) / "checkpoints"

    def list_checkpoints(self, project_id: str | None, conversation_id: str) -> list[str]:
        base = self._checkpoints_dir(project_id, conversation_id)
        if not base.is_dir():
            return []
        return sorted(
            p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("checkpoint-")
        )

    def latest_checkpoint(self, project_id: str | None, conversation_id: str) -> str | None:
        names = self.list_checkpoints(project_id, conversation_id)
        return names[-1] if names else None

    def _next_id(self, project_id: str | None, conversation_id: str) -> str:
        existing = self.list_checkpoints(project_id, conversation_id)
        n = 0
        for name in existing:
            try:
                n = max(n, int(name.removeprefix("checkpoint-")))
            except ValueError:
                continue
        return f"checkpoint-{n + 1:05d}"

    def create(
        self,
        project_id: str | None,
        conversation_id: str,
        *,
        tasks: list[dict] | None = None,
        git_state: dict | None = None,
        runtime_state: dict | None = None,
    ) -> Checkpoint:
        checkpoint_id = self._next_id(project_id, conversation_id)
        target = self._checkpoints_dir(project_id, conversation_id) / checkpoint_id

        summary = load_summary(self._conv_dir(project_id, conversation_id))
        write_json_atomic(target / "summary.json", asdict(summary))

        messages = self._store.read_messages(project_id, conversation_id)[-_WORKING_CONTEXT_TAIL:]
        working_context = [
            {"role": str(m.role), "text": m.text, "provider_id": m.provider_id} for m in messages
        ]
        write_json_atomic(target / "working-context.json", {"messages": working_context})

        write_json_atomic(target / "tasks.json", {"tasks": tasks or []})
        write_json_atomic(target / "git-state.json", git_state or {})

        bindings = self._store.provider_bindings(project_id, conversation_id)
        write_json_atomic(
            target / "provider-binding.json", {"bindings": [asdict(b) for b in bindings]}
        )
        write_json_atomic(target / "runtime-state.json", runtime_state or {})

        return Checkpoint(
            id=checkpoint_id,
            conversation_id=conversation_id,
            summary=summary.current_state,
            created_at=time.time(),
            project_id=project_id,
        )

    def load(
        self, project_id: str | None, conversation_id: str, checkpoint_id: str | None = None
    ) -> dict[str, Any] | None:
        cp_id = checkpoint_id or self.latest_checkpoint(project_id, conversation_id)
        if cp_id is None:
            return None
        target = self._checkpoints_dir(project_id, conversation_id) / cp_id
        if not target.is_dir():
            return None
        return {name: read_json(target / f"{name}.json") for name in _FILES} | {"id": cp_id}
