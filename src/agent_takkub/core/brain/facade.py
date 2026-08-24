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
from collections.abc import Mapping

from agent_takkub.core.models.memory import MemoryRecord, Scope

from . import context_builder, context_gate
from .candidate import MemoryCandidate
from .flag import v2_brain_enabled, v2_context_enabled
from .pipeline import MemoryManager, SubmitResult
from .retrieval import RetrievalEngine
from .store import BrainStore

_log = logging.getLogger(__name__)

# Closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md`'s "If a small task used 15k+
# context, flag it as inefficient" — checked only for `task_size == "small"`
# (medium/large tasks are meant to spend more).
_INEFFICIENT_SMALL_TOKENS = 15_000


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
    flags: Mapping[str, object] | None = None,
) -> str:
    """Context-Injection hook (#309 Phase 7c) — `orchestrator._assign_
    dispatch`'s call site. Meant to run inside a timeout-bounded background
    thread (a stuck/slow recall must never delay a spawn — see the hook's
    own comment in orchestrator.py); this function itself is plain sync so
    it stays trivially unit-testable without a thread pool in the loop.

    Closeout #C (`context_gate.py`) sits in front of the original assembly
    below: `TAKKUB_CONTEXT_GATE=0` skips it entirely and reproduces the
    exact pre-gate path byte-for-byte (unclamped budget, OpenViking/
    Resource always attempted whenever the sidecar itself is on) — `flags`
    (an explicit `{"context": "small"|"medium"|"large"}` override) is only
    ever consulted when the gate is on.
    """
    if not v2_context_enabled():
        return ""
    gate_on = context_gate.gate_enabled()
    task_size: context_gate.TaskSize | None = None
    policy: context_gate.SourcePolicy | None = None
    try:
        base_budget = context_builder.budget_tokens_for(
            context_window, file_read_supported=file_read_supported
        )
        if gate_on:
            task_size = context_gate.classify_task_size(task_text, role, flags)
            policy = context_gate.policy_for(task_size)
            budget = context_gate.gate_budget(task_size, base_budget)
        else:
            budget = base_budget
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
    # default — so this can never change the pre-#372 return value. The
    # gate additionally skips this call outright for a `policy` that
    # disallows reference sources (small tasks) — cheaper than calling in
    # and discarding the result, and keeps `OpenVikingSource`/
    # `ResourceSource` off a small task's critical path entirely.
    trace = None
    if policy is None or policy.allow_reference_sources:
        try:
            text, trace = context_builder.merge_openviking_traced(
                text, project=project, role=role, task_text=task_text, budget_tokens=budget
            )
        except Exception:
            _log.exception(
                "core.brain.facade.build_context_for_assign: openviking merge failed "
                "role=%r project=%r (fail-open to the pre-merge context)",
                role,
                project,
            )
    if gate_on:
        _save_gate_trace(
            text, trace, task_size=task_size, budget=budget, project=project, role=role
        )
    elif trace is not None:
        from agent_takkub.core.context_sources.trace_store import save_last_trace

        save_last_trace(trace, project=project, role=role)
    return text


def _save_gate_trace(
    text: str,
    ov_trace,
    *,
    task_size: context_gate.TaskSize | None,
    budget: int,
    project: str | None,
    role: str,
) -> None:
    """Persist a Context Gate trace unconditionally when the gate is on —
    unlike the pre-gate OpenViking-only trace (still the only thing saved
    when the gate is off, see the caller), so `doctor`'s `[context]` section
    sees the task-size decision and total tokens even for a small task that
    skipped OpenViking/Resource entirely. Best-effort: never raises into
    the caller, same contract `trace_store.save_last_trace` already has."""
    try:
        from agent_takkub.core.context_sources.base import estimate_tokens
        from agent_takkub.core.context_sources.trace_store import save_last_trace

        if ov_trace is not None:
            sources = ov_trace.sources
            total_tokens = ov_trace.total_tokens
            dedup_count = ov_trace.dedup_count
            latency_ms = ov_trace.latency_ms
            mode = f"gated:{task_size}:{ov_trace.mode}"
        else:
            sources = ()
            total_tokens = estimate_tokens(text) if text else 0
            dedup_count = 0
            latency_ms = 0.0
            mode = f"gated:{task_size}"

        trace = context_builder.ContextTrace(
            mode=mode,
            sources=tuple(sources),
            total_tokens=total_tokens,
            budget_tokens=budget,
            dedup_count=dedup_count,
            latency_ms=latency_ms,
        )
        inefficient = task_size == "small" and total_tokens > _INEFFICIENT_SMALL_TOKENS
        save_last_trace(
            trace, project=project, role=role, task_size=task_size, inefficient=inefficient
        )
    except Exception:
        _log.debug("core.brain.facade: gate trace save failed (best-effort)", exc_info=True)


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
