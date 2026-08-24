"""The Second Brain façade — `TAKKUB_V2_BRAIN` on by default since 1.0.84,
fail-open (plan §0 rules 3+4). The stable entry point `orchestrator.py`'s hooks call
into instead of reaching into `pipeline`/`retrieval`/`context_builder`
directly: `on_pane_done` (Reflection hook, Phase 7c) and
`build_context_for_assign` (Context-Injection hook, Phase 7c) join
`recall`/`submit` (Phase 7b) here.

Flag OFF: `recall()` returns `[]`, `submit()`/`on_pane_done()` are no-ops —
zero `MemoryManager`/`RetrievalEngine`/`BrainStore` construction, so a
disabled Second Brain touches no disk. Flag ON: any exception anywhere in
the path falls back to the same empty/no-op result instead of ever raising
into a caller (e.g. a spawn or `done()` path).

`build_context_for_assign` answers to a SEPARATE flag, `TAKKUB_V2_CONTEXT`
(see `flag.py`'s docstring for why) — it is fail-open the same way, but its
own flag check happens first so it stays a true no-op independent of
`TAKKUB_V2_BRAIN`.
"""

from __future__ import annotations

import logging

from agent_takkub.core.models.memory import MemoryRecord, Scope

from . import context_builder
from .candidate import MemoryCandidate
from .flag import v2_brain_enabled, v2_context_enabled
from .pipeline import MemoryManager, SubmitResult
from .retrieval import RetrievalEngine
from .store import BrainStore

_log = logging.getLogger(__name__)


def recall(
    query: str, *, scope: Scope = Scope.PROJECT, project: str | None = None, limit: int = 10
) -> list[MemoryRecord]:
    if not v2_brain_enabled():
        return []
    try:
        engine = RetrievalEngine(BrainStore(project))
        return list(engine.recall(query, scope=scope)[:limit])
    except Exception:
        _log.exception(
            "core.brain.facade.recall failed query=%r scope=%r project=%r (fail-open)",
            query,
            scope,
            project,
        )
        return []


def submit(candidate: MemoryCandidate, *, project: str | None = None) -> SubmitResult | None:
    if not v2_brain_enabled():
        return None
    try:
        manager = MemoryManager(BrainStore(project))
        return manager.submit_candidate(candidate)
    except Exception:
        _log.exception(
            "core.brain.facade.submit failed source=%r project=%r (fail-open)",
            candidate.source,
            project,
        )
        return None


def build_context_for_assign(
    project: str | None,
    role: str,
    task_text: str,
    *,
    context_window: int | None = None,
    file_read_supported: bool = True,
) -> str:
    """Context-Injection hook (#309 Phase 7c) — `orchestrator._assign_
    dispatch`'s call site. Meant to run inside a timeout-bounded background
    thread (a stuck/slow recall must never delay a spawn — see the hook's
    own comment in orchestrator.py); this function itself is plain sync so
    it stays trivially unit-testable without a thread pool in the loop.
    """
    if not v2_context_enabled():
        return ""
    try:
        budget = context_builder.budget_tokens_for(
            context_window, file_read_supported=file_read_supported
        )
        text = context_builder.build_context(project, role, task_text, budget)
    except Exception:
        _log.exception(
            "core.brain.facade.build_context_for_assign failed role=%r project=%r (fail-open)",
            role,
            project,
        )
        return ""
    # OpenViking hybrid merge (#372) — a SEPARATE fail-open boundary from
    # the one above: a bug here must fall back to the perfectly good Brain/
    # Conversation `text` already built, not discard it and return "".
    # No-op (same `text` back) whenever OpenViking is disabled — the
    # default — so this can never change the pre-#372 return value.
    try:
        text, trace = context_builder.merge_openviking_traced(
            text, project=project, role=role, task_text=task_text, budget_tokens=budget
        )
        if trace is not None:
            from agent_takkub.core.context_sources.trace_store import save_last_trace

            save_last_trace(trace, project=project, role=role)
    except Exception:
        _log.exception(
            "core.brain.facade.build_context_for_assign: openviking merge failed "
            "role=%r project=%r (fail-open to the pre-merge context)",
            role,
            project,
        )
    return text


def on_pane_done(
    project: str | None,
    role: str,
    *,
    note: str,
    digest_facts: object | None = None,
    failed: bool = False,
    task_id: str | None = None,
) -> None:
    """Reflection hook (#309 Phase 7c) — `orchestrator.done()`/`subagent_
    done()`'s call site (same `TAKKUB_V2_BRAIN` flag that already gates
    `recall`/`submit`; this is just another write path into the same Second
    Brain, not a separate feature). `digest_facts` is a `digest_facts.
    DigestFacts` — typed loosely here so this module never has to import
    that top-level module at load time (only inside the try, and only when
    a caller actually passed one)."""
    if not v2_brain_enabled():
        return
    try:
        from .sources.reflection_source import decisions_from_note, from_done_note

        note_candidate = from_done_note(
            note, project=project, role=role, failed=failed, task_id=task_id
        )
        if note_candidate is not None:
            submit(note_candidate, project=project)

        for decision_candidate in decisions_from_note(
            note, project=project, role=role, task_id=task_id
        ):
            submit(decision_candidate, project=project)

        if digest_facts is not None and project is not None:
            from .sources.digest_facts_source import from_digest_facts

            submit(from_digest_facts(digest_facts, project=project), project=project)
    except Exception:
        _log.exception(
            "core.brain.facade.on_pane_done failed role=%r project=%r (fail-open)", role, project
        )


__all__ = ["build_context_for_assign", "on_pane_done", "recall", "submit"]
