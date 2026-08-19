"""The ONE façade `orchestrator.py`'s `done()`/`subagent_done()` — and
`core.accounts.selector.CooldownFailoverSelector`'s account-switch path —
call into (plan §0 rule 4: "จุดเชื่อมเดียว ... fail-open").

Flag OFF (default): every function here returns immediately, touching
nothing on disk — `orchestrator.done()` stays byte-identical (proof:
`tests/test_core_conversation.py`'s flag-off no-op tests).
Flag ON: best-effort — any exception anywhere in the chain is caught and
logged, never raised into the caller. `done()`/`subagent_done()` wrap the
call in a background `threading.Thread` themselves (plan: "I/O ใน thread
... ให้ caller ฝั่ง orchestrator wrap เอง"); this module stays plain sync
so it's trivially unit-testable without a thread in the loop.
"""

from __future__ import annotations

import logging

from agent_takkub.core.models.conversation import MessageRole

from .checkpoint import CheckpointManager
from .flag import v2_conversation_enabled
from .ingest import ingest_provider_transcript
from .store import ConversationStore, conversation_id_for
from .summary import apply_done_note, load_summary, save_summary

_log = logging.getLogger(__name__)


def _resolve_account_id(provider_id: str, project_id: str | None, role: str) -> str | None:
    try:
        from agent_takkub.core.accounts.facade import resolve_account_for

        account = resolve_account_for(provider_id, project_id or "", role)
        return account.id if account is not None else None
    except Exception:
        _log.exception(
            "core.conversation.facade._resolve_account_id failed for provider=%r project=%r "
            "role=%r (fail-open — binding still created without account_id)",
            provider_id,
            project_id,
            role,
        )
        return None


def on_pane_done(
    project_id: str | None,
    role: str,
    *,
    note: str,
    failed: bool,
    task_id: str | None = None,
    provider_id: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    if not v2_conversation_enabled():
        return
    try:
        _on_pane_done_impl(
            project_id,
            role,
            note=note,
            failed=failed,
            task_id=task_id,
            provider_id=provider_id,
            cwd=cwd,
            session_id=session_id,
            store=store,
        )
    except Exception:
        _log.exception(
            "core.conversation.facade.on_pane_done failed for role=%r project=%r "
            "(fail-open — orchestrator.done() already returned)",
            role,
            project_id,
        )


def _on_pane_done_impl(
    project_id: str | None,
    role: str,
    *,
    note: str,
    failed: bool,
    task_id: str | None,
    provider_id: str | None,
    cwd: str | None,
    session_id: str | None,
    store: ConversationStore | None,
) -> None:
    cs = store or ConversationStore()
    conversation_id = conversation_id_for(project_id, role)
    cs.get_or_create(conversation_id, project_id=project_id, task_id=task_id)

    if provider_id and cwd:
        ingest_provider_transcript(
            provider_id, project_id, conversation_id, cwd=cwd, session_id=session_id, store=cs
        )

    # session_id is None for a subagent (no pane/session of its own) — skip
    # rather than record an empty/meaningless binding (plan: pointer only).
    if provider_id and session_id:
        account_id = _resolve_account_id(provider_id, project_id, role)
        cs.bind_provider_session(
            project_id,
            conversation_id,
            provider_id=provider_id,
            session_id=session_id,
            cwd=cwd,
            account_id=account_id,
        )

    if note.strip():
        cs.append_message(
            project_id,
            conversation_id,
            role=MessageRole.ASSISTANT,
            text=note,
            provider_id=provider_id,
            source="done_note",
        )

    conv_dir = cs.conversation_dir(project_id, conversation_id)
    summary = load_summary(conv_dir)
    summary = apply_done_note(summary, role=role, note=note, failed=failed)
    save_summary(conv_dir, summary)

    CheckpointManager(cs).create(project_id, conversation_id)


def on_account_switch(
    pool_id: str,
    provider_id: str,
    from_account_id: str | None,
    to_account_id: str | None,
    *,
    reason: str,
    conversation_id: str | None = None,
    project_id: str | None = None,
    store: ConversationStore | None = None,
) -> None:
    """Wired call site for "checkpoint ก่อน account switch" (plan §7). No
    account is bound to a conversation id yet (gap — `core.accounts` has no
    account->conversation link, see `docs/v2/phase6-report.md`), so today
    this only fires when a caller explicitly supplies one; without it, it is
    a documented no-op rather than a guess at which conversation to
    snapshot.
    """
    if not v2_conversation_enabled() or conversation_id is None:
        return
    try:
        cs = store or ConversationStore()
        if cs.get_conversation(project_id, conversation_id) is None:
            return
        CheckpointManager(cs).create(
            project_id,
            conversation_id,
            runtime_state={
                "trigger": "account_switch",
                "pool_id": pool_id,
                "provider_id": provider_id,
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "reason": reason,
            },
        )
    except Exception:
        _log.exception(
            "core.conversation.facade.on_account_switch failed for pool=%r (fail-open)", pool_id
        )
